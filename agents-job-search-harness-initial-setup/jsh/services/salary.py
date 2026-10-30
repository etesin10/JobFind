"""Salary normalisation: every salary → per-YEAR amounts in ONE base currency (default CAD), with the exchange rate,
its date and source recorded, and every assumption made explicit.

- Periods: year/month/biweekly/week/day/hour (EN/ES aliases). Hourly uses ``hours_per_year`` (default 2080) and
  daily ``days_per_year`` (260). Unknown period → NOT normalised (shown as "periodo desconocido").
- Currency: the job's currency; when empty it is ASSUMED from the job country (CA→CAD, MX→MXN, US→USD…) and flagged.
- Rates: European Central Bank daily reference rates via api.frankfurter.dev (free, no key), fetched only when the
  user asks or when they are older than ``max_age_days``. Any rate can be overridden manually (source "manual");
  automatic refreshes never overwrite manual rates.
"""
from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any

import httpx
from sqlalchemy import delete, select

from ..connectors.base import ipv4_transport, ssl_context
from ..models import AuditEvent, FxRate, Job, utcnow
from ..normalize import normalize_name

log = logging.getLogger(__name__)

BASE_KEY, HOURS_KEY, DAYS_KEY, MAX_AGE_KEY = ("salary_base_currency", "salary_hours_per_year",
                                              "salary_days_per_year", "fx_max_age_days")
AUTO_KEY = "fx_auto_refresh"
UNAVAILABLE_KEY = "fx_unavailable"  # currencies the ECB did not return in the last refresh
DEFAULT_BASE = "CAD"
FX_URL = "https://api.frankfurter.dev/v2/rates"
COMMON = ("USD", "MXN", "CAD", "EUR", "GBP")
COUNTRY_CURRENCY = {"CA": "CAD", "MX": "MXN", "US": "USD", "GB": "GBP", "IE": "EUR", "ES": "EUR", "DE": "EUR",
                    "FR": "EUR", "NL": "EUR", "PT": "EUR", "IT": "EUR", "CO": "COP", "AR": "ARS", "CL": "CLP",
                    "PE": "PEN", "BR": "BRL", "IN": "INR", "AU": "AUD", "NZ": "NZD", "CH": "CHF", "SE": "SEK",
                    "PL": "PLN", "JP": "JPY", "SG": "SGD"}
_PERIODS = {
    "year": ("year", "yearly", "annual", "annually", "annum", "per year", "yr", "y", "año", "anual", "anio",
             "al año", "por año"),
    "month": ("month", "monthly", "per month", "mo", "mes", "mensual", "al mes", "por mes"),
    "biweekly": ("biweekly", "bi-weekly", "fortnight", "fortnightly", "quincena", "quincenal"),
    "week": ("week", "weekly", "per week", "wk", "semana", "semanal"),
    "day": ("day", "daily", "per day", "día", "dia", "diario", "por día"),
    "hour": ("hour", "hourly", "per hour", "hr", "h", "hora", "por hora"),
}
PERIOD_ALIASES = {normalize_name(a): k for k, aliases in _PERIODS.items() for a in aliases}
PERIOD_LABELS = {"year": "año", "month": "mes", "biweekly": "quincena", "week": "semana", "day": "día",
                 "hour": "hora"}


def canonical_period(text: str | None) -> str:
    return PERIOD_ALIASES.get(normalize_name(text or ""), "")


def period_factor(period: str, hours_per_year: float = 2080, days_per_year: float = 260) -> float | None:
    return {"year": 1.0, "month": 12.0, "biweekly": 26.0, "week": 52.0, "day": float(days_per_year),
            "hour": float(hours_per_year)}.get(period)


def normalize_salary(salary_min: float | None, salary_max: float | None, currency: str, period: str,
                     job_country: str, base: str, rates: dict[str, dict[str, Any]],
                     hours_per_year: float = 2080, days_per_year: float = 260) -> dict[str, Any]:
    """Pure function. ``rates``: currency → {"rate", "as_of", "source"} meaning 1 currency = rate × base."""
    out: dict[str, Any] = {"status": "no_salary", "min": None, "max": None, "base": base, "rate": None,
                           "as_of": "", "source": "", "assumptions": [], "currency": "", "period": ""}
    if salary_min is None and salary_max is None:
        return out
    cur = (currency or "").strip().upper()
    if not cur:
        cur = COUNTRY_CURRENCY.get((job_country or "").upper(), "")
        if not cur:
            out["status"] = "unknown_currency"
            return out
        out["assumptions"].append(f"moneda supuesta por país ({job_country} → {cur})")
        out["assumption_codes"] = [("currency", job_country, cur)]
    out["currency"] = cur
    per = canonical_period(period)
    if not per:
        out["status"] = "unknown_period"
        return out
    out["period"] = per
    factor = period_factor(per, hours_per_year, days_per_year)
    if per == "hour":
        out["assumptions"].append(f"{hours_per_year:g} horas/año")
        out.setdefault("assumption_codes", []).append(("hours", hours_per_year))
    elif per == "day":
        out["assumptions"].append(f"{days_per_year:g} días/año")
        out.setdefault("assumption_codes", []).append(("days", days_per_year))
    if cur == base:
        rate, as_of, source = 1.0, "", "misma moneda"
    else:
        info = rates.get(cur)
        if not info:
            out["status"] = "no_rate"
            return out
        rate, as_of, source = float(info["rate"]), str(info.get("as_of") or ""), str(info.get("source") or "")
    conv = factor * rate
    out.update(status="ok", rate=rate, as_of=as_of, source=source,
               min=round(salary_min * conv) if salary_min is not None else None,
               max=round(salary_max * conv) if salary_max is not None else None)
    return out


def describe(n: dict[str, Any]) -> str:
    """Short human text (interface language): '≈ 85k–110k CAD/año (USD @1.3912, 2026-10-30, BCE)' or why it could
    not be normalised."""
    from ..i18n import t
    st = n["status"]
    if st == "no_salary":
        return ""
    if st == "unknown_currency":
        return t("Moneda desconocida")
    if st == "unknown_period":
        return t("Periodo salarial desconocido (no se normaliza)")
    if st == "no_rate":
        return t("Sin tipo de cambio {}→{} (Configuración → Moneda)", n["currency"], n["base"])

    def k(v):
        return f"{v / 1000:.0f}k" if v is not None and abs(v) >= 10000 else (f"{v:,.0f}" if v is not None else "?")
    rng = k(n["min"]) if n["max"] is None or n["max"] == n["min"] else (
        k(n["max"]) if n["min"] is None else f"{k(n['min'])}–{k(n['max'])}")
    extra = []
    if n["currency"] != n["base"]:
        extra.append(f"{n['currency']} @{n['rate']:.4g}" + (f", {n['as_of']}" if n["as_of"] else "")
                     + (f", {t(SOURCE_LABELS.get(n['source'], n['source']))}" if n["source"] else ""))
    for code in n.get("assumption_codes", []):
        if code[0] == "currency":
            extra.append(t("moneda supuesta por país ({} → {})", code[1], code[2]))
        elif code[0] == "hours":
            extra.append(t("{:g} horas/año", code[1]))
        elif code[0] == "days":
            extra.append(t("{:g} días/año", code[1]))
    return t("≈ {} {}/año", rng, n["base"]) + (f" ({'; '.join(extra)})" if extra else "")


SOURCE_LABELS = {"ecb": "BCE", "manual": "manual", "misma moneda": ""}


class SalaryService:
    def __init__(self, app, transport: httpx.BaseTransport | None = None):
        self.app = app
        self.db = app.db
        self.transport = transport  # tests inject httpx.MockTransport
        self._cache: tuple[str, dict[str, dict[str, Any]]] | None = None
        self._params: tuple[str, float, float] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ settings
    def base(self) -> str:
        return str(self.db.get_setting(BASE_KEY, DEFAULT_BASE) or DEFAULT_BASE).upper()

    def hours_per_year(self) -> float:
        return float(self.db.get_setting(HOURS_KEY, 2080) or 2080)

    def days_per_year(self) -> float:
        return float(self.db.get_setting(DAYS_KEY, 260) or 260)

    def max_age_days(self) -> int:
        return int(self.db.get_setting(MAX_AGE_KEY, 7) or 7)

    def configure(self, base: str | None = None, hours_per_year: float | None = None,
                  days_per_year: float | None = None, actor: str = "user") -> None:
        if base is not None:
            base = base.strip().upper()
            if len(base) != 3 or not base.isalpha():
                raise ValueError("La moneda base debe ser un código ISO de 3 letras (CAD, MXN, USD…).")
            self.db.set_setting(BASE_KEY, base)
        if hours_per_year is not None:
            if not 500 <= float(hours_per_year) <= 4000:
                raise ValueError("Horas por año fuera de rango (500–4000).")
            self.db.set_setting(HOURS_KEY, float(hours_per_year))
        if days_per_year is not None:
            if not 100 <= float(days_per_year) <= 366:
                raise ValueError("Días por año fuera de rango (100–366).")
            self.db.set_setting(DAYS_KEY, float(days_per_year))
        self._changed(actor, "salary_settings", base=self.base(), hours=self.hours_per_year(),
                      days=self.days_per_year())

    # ------------------------------------------------------------------ rates
    def rates(self) -> dict[str, dict[str, Any]]:
        return self._rates_for(self.base())

    def _rates_for(self, base: str) -> dict[str, dict[str, Any]]:
        cached = self._cache
        if cached is not None and cached[0] == base:
            return cached[1]
        with self.db.session() as s:
            data = {r.currency: {"rate": r.rate, "as_of": r.as_of.isoformat() if r.as_of else "",
                                 "source": r.source, "updated_at": r.updated_at.isoformat(timespec="seconds")}
                    for r in s.scalars(select(FxRate).where(FxRate.base == base))}
        self._cache = (base, data)
        return data

    def rate_rows(self) -> list[dict[str, Any]]:
        return [{"currency": c, **v} for c, v in sorted(self.rates().items())]

    def currencies_in_use(self, include_common: bool = True) -> list[str]:
        with self.db.session() as s:
            rows = s.execute(select(Job.currency, Job.job_country).where(
                (Job.salary_min.is_not(None)) | (Job.salary_max.is_not(None)))).all()
        found = {(c or "").upper() or COUNTRY_CURRENCY.get((jc or "").upper(), "") for c, jc in rows}
        return sorted({c for c in found if len(c) == 3 and c.isalpha()} | (set(COMMON) if include_common else set()))

    def stale(self) -> bool:
        """True when a currency used by a saved salary has no rate, or an ECB rate is older than max_age_days.
        Currencies the ECB does not publish (COP, ARS…) are ignored here: they need a manual rate."""
        rates = self.rates()
        unavailable = set(self.db.get_setting(UNAVAILABLE_KEY, []) or [])
        needed = [c for c in self.currencies_in_use(include_common=False) if c != self.base()
                  and c not in unavailable]
        if not needed:
            return False
        if any(c not in rates for c in needed):
            return True
        today = date.today()
        return any(v["source"] != "manual" and v["as_of"] and (today - date.fromisoformat(v["as_of"])).days >
                   self.max_age_days() for v in rates.values())

    def refresh_rates(self, actor: str = "user") -> dict[str, Any]:
        """Fetch ECB rates for the currencies in use. Manual rates are never overwritten."""
        base = self.base()
        wanted = [c for c in self.currencies_in_use() if c != base]
        if not wanted:
            return {"updated": 0, "skipped_manual": [], "missing": [], "as_of": ""}
        params = {"base": base, "quotes": ",".join(wanted), "providers": "ecb"}
        with httpx.Client(timeout=20, verify=ssl_context(), follow_redirects=False,
                          transport=self.transport or ipv4_transport(),
                          headers={"User-Agent": "JobSearchHarness (fx rates; personal use)"}) as client:
            resp = client.get(FX_URL, params=params)
        if resp.status_code != 200:
            raise ValueError(f"No se pudieron obtener tipos de cambio (HTTP {resp.status_code}).")
        data = resp.json()
        items = data if isinstance(data, list) else []
        got: dict[str, tuple[float, str]] = {}
        for it in items:
            try:
                quote, rate, day = str(it["quote"]).upper(), float(it["rate"]), str(it["date"])[:10]
                date.fromisoformat(day)
            except (KeyError, TypeError, ValueError):
                continue
            if it.get("base", base).upper() == base and rate > 0:
                got[quote] = (1.0 / rate, day)  # API gives 1 base = rate quote → we store 1 quote = x base
        skipped: list[str] = []
        changed = False
        with self.db.session() as s:
            existing = {r.currency: r for r in s.scalars(select(FxRate).where(FxRate.base == base))}
            for cur, (rate, day) in got.items():
                row = existing.get(cur)
                if row is not None and row.source == "manual":
                    skipped.append(cur)
                    continue
                if row is None:
                    s.add(FxRate(base=base, currency=cur, rate=rate, as_of=date.fromisoformat(day), source="ecb"))
                    changed = True
                else:
                    if abs(row.rate - rate) > 1e-12 or row.as_of != date.fromisoformat(day) or row.source != "ecb":
                        changed = True
                    row.rate, row.as_of, row.source, row.updated_at = rate, date.fromisoformat(day), "ecb", utcnow()
            s.commit()
        missing = [c for c in wanted if c not in got]
        self.db.set_setting(UNAVAILABLE_KEY, missing)
        res = {"updated": len(got) - len(skipped), "skipped_manual": skipped,
               "missing": missing, "as_of": max((d for _, d in got.values()), default="")}
        if changed:  # re-evaluate (and audit) only when a rate actually changed
            self._changed(actor, "fx_refreshed", **res)
        else:
            self._cache = None
        return res

    def refresh_if_stale(self) -> dict[str, Any] | None:
        if not self.db.get_setting(AUTO_KEY, True) or not self.stale():
            return None
        try:
            return self.refresh_rates(actor="fx-auto")
        except Exception as exc:  # offline / proxy: keep the old rates
            log.info("FX refresh skipped: %s", exc)
            return None

    def set_manual_rate(self, currency: str, rate: float, as_of: date | str | None = None,
                        actor: str = "user") -> None:
        cur = (currency or "").strip().upper()
        if len(cur) != 3 or not cur.isalpha() or cur == self.base():
            raise ValueError("Moneda inválida (código ISO de 3 letras, distinta de la moneda base).")
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            raise ValueError("Tipo de cambio inválido.") from None
        if not rate > 0:
            raise ValueError("El tipo de cambio debe ser mayor que 0.")
        day = as_of if isinstance(as_of, date) else (date.fromisoformat(str(as_of)[:10]) if as_of else date.today())
        base = self.base()
        with self.db.session() as s:
            row = s.scalar(select(FxRate).where(FxRate.base == base, FxRate.currency == cur))
            if row is None:
                s.add(FxRate(base=base, currency=cur, rate=rate, as_of=day, source="manual"))
            else:
                row.rate, row.as_of, row.source, row.updated_at = rate, day, "manual", utcnow()
            s.commit()
        self._changed(actor, "fx_manual", currency=cur, rate=rate, as_of=day.isoformat())

    def delete_rate(self, currency: str, actor: str = "user") -> None:
        with self.db.session() as s:
            s.execute(delete(FxRate).where(FxRate.base == self.base(), FxRate.currency == currency.upper()))
            s.commit()
        self._changed(actor, "fx_deleted", currency=currency.upper())

    def _changed(self, actor: str, action: str, **details) -> None:
        self.invalidate()
        with self.db.session() as s:
            s.add(AuditEvent(entity_type="settings", entity_id="salary", action=action, actor=actor[:60],
                             details=details))
            s.commit()
        for mode in ("CANADA_VISA", "MEXICO_REMOTE"):  # rules may use the normalised salary
            self.app.jobs.reevaluate_mode(mode)

    # ------------------------------------------------------------------ normalisation
    def invalidate(self) -> None:
        self._cache = None
        self._params = None

    def normalize_job(self, job: Any) -> dict[str, Any]:
        """``job``: Job or row-like object/dict with salary_min/max, currency, salary_period, job_country."""
        get = (lambda k: job.get(k)) if isinstance(job, dict) else (lambda k: getattr(job, k, None))
        params = self._params
        if params is None:  # settings read once, not per job (invalidated by _changed)
            params = self._params = (self.base(), self.hours_per_year(), self.days_per_year())
        base, hours, days = params
        return normalize_salary(get("salary_min"), get("salary_max"), get("currency") or "",
                                get("salary_period") or "", get("job_country") or "", base, self._rates_for(base),
                                hours, days)
