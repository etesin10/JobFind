"""Amazon (amazon.jobs) connector.

Amazon does not use Greenhouse/Lever/Ashby and publishes no documented public jobs API. This connector uses
the same public JSON search that the amazon.jobs website itself calls (``/en/search.json``). Its robots.txt
(checked 2026-10-29) only disallows ``/internal`` paths. We stay respectful: identified User-Agent, ≥ 1.5 s
between requests, small page counts, no login, no apply automation. If Amazon blocks or changes it, the
connector reports the error and the manual URL/CSV import keeps working.

Facts recorded from Amazon's own data (not guessed):
- location type ONSITE → work_mode "onsite"; VIRTUAL/"Virtual" → "remote", eligible ONLY for that country.
- legal hiring entity (e.g. "Amazon Development Centre Canada ULC") kept in extra.legal_entity: relevant
  for employer-specific Canadian work permits.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..services.jobs import JobInput
from .base import ISO2_TO_ISO3, ConnectorError, FetchResult, HttpClient, SourceQuery, html_to_text

BASE = "https://www.amazon.jobs"
PAGE_SIZE = 100
MAX_RESULTS_CAP = 500
_ISO3_TO_ISO2 = {v: k for k, v in ISO2_TO_ISO3.items()}


class AmazonConnector:
    provider = "amazon_jobs"
    label = "Amazon (amazon.jobs)"
    hosts = {"www.amazon.jobs"}
    min_interval = 1.5
    official_api = False
    needs_identifier = False
    policy_note = ("Búsqueda pública de amazon.jobs (sin API documentada; robots.txt la permite). Uso moderado: "
                   "1 consulta cada 1.5 s, máximo 500 resultados por fuente.")

    def fetch(self, q: SourceQuery, http: HttpClient) -> FetchResult:
        result = FetchResult()
        countries = []
        for c in q.countries:
            c = c.strip().upper()
            iso3 = ISO2_TO_ISO3.get(c) or (c if len(c) == 3 and c.isalpha() else None)
            if not iso3:
                raise ConnectorError(f"País no reconocido para Amazon: '{c}'. Use códigos como CA, MX, US.")
            countries.append(iso3)
        limit = max(1, min(int(q.max_results or 100), MAX_RESULTS_CAP))
        query = " ".join(k.strip() for k in q.keywords if k.strip())
        offset = 0
        seen: set[str] = set()
        while offset < limit:
            params: list[tuple[str, Any]] = [("base_query", query), ("result_limit", min(PAGE_SIZE, limit - offset)),
                                             ("offset", offset), ("sort", "recent")]
            params += [("normalized_country_code[]", c) for c in countries]
            try:
                data = http.get_json(f"{BASE}/en/search.json", params=params)
            except ConnectorError:
                if result.jobs:
                    result.partial = True
                    result.warnings.append(f"Se detuvo en el resultado {offset}: la página siguiente falló.")
                    break
                raise
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                raise ConnectorError("Amazon cambió el formato de su búsqueda pública (falta 'jobs').")
            if data.get("error"):
                raise ConnectorError(f"Amazon devolvió un error en la búsqueda: {str(data['error'])[:120]}")
            result.total_available = data.get("hits") if isinstance(data.get("hits"), int) else None
            page = data["jobs"]
            for raw in page:
                try:
                    job = self._to_job(raw)
                except (KeyError, TypeError, ValueError) as exc:
                    result.warnings.append(f"Vacante omitida (formato inesperado: {type(exc).__name__}).")
                    continue
                if job.source_job_id not in seen:
                    seen.add(job.source_job_id)
                    result.jobs.append(job)
            offset += len(page)
            if not page or (result.total_available is not None and offset >= result.total_available):
                break
        result.requests = http.requests
        if result.total_available is not None and result.total_available > limit:
            result.warnings.append(f"Amazon reporta {result.total_available} resultados; se trajeron los primeros "
                                   f"{len(result.jobs)} (máximo configurado {limit}). Refine las palabras clave.")
        return result

    @staticmethod
    def _locations(raw: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for item in raw.get("locations") or []:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except ValueError:
                    continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def _to_job(self, raw: dict[str, Any]) -> JobInput:
        job_id = str(raw["id_icims"] or raw["id"])
        locs = self._locations(raw)
        loc_text = raw.get("normalized_location") or raw.get("location") or ""
        onsite_countries, remote_countries = [], []
        for loc in locs:
            iso2 = (loc.get("countryIso2a") or _ISO3_TO_ISO2.get(loc.get("countryIso3a") or "", "")).upper()
            virtual = str(loc.get("type", "")).upper() == "VIRTUAL" or "virtual" in str(loc.get("location", "")).lower()
            if iso2:
                (remote_countries if virtual else onsite_countries).append(iso2)
        if not locs:
            iso2 = _ISO3_TO_ISO2.get(str(raw.get("country_code") or "").upper(), "")
            virtual = "virtual" in str(raw.get("location", "")).lower()
            if iso2:
                (remote_countries if virtual else onsite_countries).append(iso2)
        if remote_countries and not onsite_countries:
            work_mode = "remote"
        elif remote_countries and onsite_countries:
            work_mode = "hybrid"
        elif onsite_countries:
            work_mode = "onsite"
        else:
            work_mode = ""
        sections = [html_to_text(raw.get("description"))]
        if raw.get("basic_qualifications"):
            sections.append("BASIC QUALIFICATIONS\n" + html_to_text(raw["basic_qualifications"]))
        if raw.get("preferred_qualifications"):
            sections.append("PREFERRED QUALIFICATIONS\n" + html_to_text(raw["preferred_qualifications"]))
        posted = None
        if raw.get("posted_date"):
            try:
                posted = datetime.strptime(" ".join(str(raw["posted_date"]).split()), "%B %d, %Y").date()
            except ValueError:
                posted = None
        path = str(raw.get("job_path") or f"/en/jobs/{job_id}")
        if not path.startswith("/"):
            raise ValueError("job_path")
        apply = str(raw.get("url_next_step") or "")
        return JobInput(
            title=str(raw["title"])[:300], company_name="Amazon", source=self.provider, source_job_id=job_id,
            canonical_url=BASE + path, apply_url=apply if apply.startswith("https://") else "",
            job_country=(onsite_countries or remote_countries or [""])[0], region=str(raw.get("state") or "")[:120],
            location_text=loc_text[:300], work_mode=work_mode,
            remote_eligible_countries=sorted(set(remote_countries)),
            employment_type=str(raw.get("job_schedule_type") or "")[:60], posted_date=posted,
            description="\n\n".join(s for s in sections if s),
            extra={"provider": {"legal_entity": raw.get("company_name") or "",
                                "job_family": raw.get("job_family") or "", "category": raw.get("job_category") or "",
                                "business_category": raw.get("business_category") or "",
                                "locations": [loc.get("normalizedLocation") or loc.get("location") for loc in locs][:20]}},
        )
