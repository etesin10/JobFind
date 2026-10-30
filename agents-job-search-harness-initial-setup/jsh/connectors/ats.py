"""Documented public job-board APIs: Greenhouse, Lever, Ashby (spec §6).

Each needs the employer's board identifier (e.g. Greenhouse ``gitlab``); these are per-employer feeds,
NOT a universal search engine. Formats verified live on 2026-10-29.
- Greenhouse Job Board API: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
- Lever Postings API:       https://api.lever.co/v0/postings/{site}?mode=json  (EU: api.eu.lever.co)
- Ashby Posting API:        https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from ..normalize import normalize_country
from ..services.jobs import JobInput
from .base import (
    IDENTIFIER_RE, ConnectorError, FetchResult, HttpClient, SourceQuery, _US_STATE_TAIL, countries_in, html_to_text,
    keyword_match, parse_free_location,
)

MAX_RESULTS_CAP = 1000


def _US_ONLY(text: str) -> bool:
    return bool(_US_STATE_TAIL.search(text or ""))


def _identifier(q: SourceQuery, what: str) -> str:
    ident = (q.identifier or "").strip()
    if not IDENTIFIER_RE.match(ident):
        raise ConnectorError(f"Falta o es inválido el identificador de {what} (solo letras, números, - _ .).")
    return ident


def _iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, OSError, OverflowError):
        return None


def _filter(q: SourceQuery, job: JobInput) -> bool:
    if not keyword_match(q.keywords, job.title, job.description[:5000]):
        return False
    if q.countries:
        wanted = {c.upper() for c in q.countries}
        have = {job.job_country, *job.remote_eligible_countries} - {""}
        # Unknown location is kept (spec: unknown ≠ negative); known non-matching locations are dropped.
        return not have or bool(have & wanted)
    return True


def _finish(q: SourceQuery, jobs: list[JobInput], total: int, http: HttpClient) -> FetchResult:
    limit = max(1, min(int(q.max_results or 100), MAX_RESULTS_CAP))
    kept = [j for j in jobs if _filter(q, j)]
    res = FetchResult(jobs=kept[:limit], requests=http.requests, total_available=total)
    if len(kept) > limit:
        res.warnings.append(f"{len(kept)} vacantes coinciden; se importaron {limit} (máximo configurado).")
    return res


class GreenhouseConnector:
    provider = "greenhouse"
    label = "Greenhouse (tablero de empleador)"
    hosts = {"boards-api.greenhouse.io"}
    min_interval = 1.0
    official_api = True
    needs_identifier = True
    policy_note = "API pública documentada de Greenhouse (Job Board API). Requiere el 'board token' del empleador."

    def fetch(self, q: SourceQuery, http: HttpClient) -> FetchResult:
        token = _identifier(q, "Greenhouse (board token)")
        data = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ConnectorError("Greenhouse cambió su formato (falta 'jobs').")
        jobs = []
        for raw in data["jobs"]:
            try:
                loc = parse_free_location((raw.get("location") or {}).get("name", ""))
                jobs.append(JobInput(
                    title=str(raw["title"])[:300], company_name=str(raw.get("company_name") or token)[:200],
                    source=self.provider, source_job_id=str(raw["id"]), canonical_url=raw.get("absolute_url") or "",
                    description=html_to_text(raw.get("content")),
                    posted_date=_iso_date(raw.get("first_published") or raw.get("updated_at")),
                    language=str(raw.get("language") or "")[:20], **loc,
                    extra={"provider": {"board": token, "departments": [d.get("name") for d in
                                                                         raw.get("departments") or []][:5]}}))
            except (KeyError, TypeError, ValueError):
                continue
        return _finish(q, jobs, len(data["jobs"]), http)


class LeverConnector:
    provider = "lever"
    label = "Lever (tablero de empleador)"
    hosts = {"api.lever.co", "api.eu.lever.co"}
    min_interval = 1.0
    official_api = True
    needs_identifier = True
    policy_note = "API pública documentada de Lever (Postings API). Requiere el nombre del sitio del empleador."

    def fetch(self, q: SourceQuery, http: HttpClient) -> FetchResult:
        site = _identifier(q, "Lever (sitio)")
        host = "api.eu.lever.co" if site.lower().endswith(".eu") else "api.lever.co"
        site = site[:-3] if site.lower().endswith(".eu") else site
        data = http.get_json(f"https://{host}/v0/postings/{site}", params={"mode": "json"})
        if not isinstance(data, list):
            raise ConnectorError("Lever cambió su formato (se esperaba una lista).")
        jobs = []
        for raw in data:
            try:
                cats = raw.get("categories") or {}
                loc_text = cats.get("location") or ", ".join(cats.get("allLocations") or [])
                structured = normalize_country(raw.get("country") or "")
                loc = parse_free_location(loc_text, structured if len(structured) == 2 else "")
                country = loc["job_country"]
                wpt = str(raw.get("workplaceType") or "").lower()
                work_mode = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(wpt, loc["work_mode"])
                # Remote posting tied to a stated country → remote within that country (never "worldwide").
                remote_countries = (loc["remote_eligible_countries"] or ([country] if country else [])) \
                    if work_mode == "remote" else []
                body = [raw.get("descriptionPlain") or html_to_text(raw.get("description"))]
                for lst in raw.get("lists") or []:
                    body.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
                if raw.get("additionalPlain"):
                    body.append(raw["additionalPlain"])
                sal = raw.get("salaryRange") or {}
                jobs.append(JobInput(
                    title=str(raw["text"])[:300], company_name=site, source=self.provider,
                    source_job_id=str(raw["id"]), canonical_url=raw.get("hostedUrl") or "",
                    apply_url=raw.get("applyUrl") or "", description="\n\n".join(b for b in body if b),
                    posted_date=_iso_date(raw.get("createdAt")), location_text=loc_text[:300], job_country=country,
                    work_mode=work_mode, remote_eligible_countries=remote_countries,
                    employment_type=str(cats.get("commitment") or "")[:60],
                    salary_min=sal.get("min"), salary_max=sal.get("max"), currency=sal.get("currency") or "",
                    salary_period={"per-year-salary": "year", "per-month-salary": "month",
                                   "per-hour-wage": "hour"}.get(sal.get("interval") or "", ""),
                    extra={"provider": {"site": site, "team": cats.get("team") or ""}}))
            except (KeyError, TypeError, ValueError):
                continue
        return _finish(q, jobs, len(data), http)


class AshbyConnector:
    provider = "ashby"
    label = "Ashby (tablero de empleador)"
    hosts = {"api.ashbyhq.com"}
    min_interval = 1.0
    official_api = True
    needs_identifier = True
    policy_note = "API pública documentada de Ashby (Posting API). Requiere el nombre del tablero del empleador."

    def fetch(self, q: SourceQuery, http: HttpClient) -> FetchResult:
        org = _identifier(q, "Ashby (tablero)")
        data = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}",
                             params={"includeCompensation": "true"})
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ConnectorError("Ashby cambió su formato (falta 'jobs').")
        jobs = []
        for raw in data["jobs"]:
            try:
                if raw.get("isListed") is False:
                    continue
                names = [raw.get("location") or ""] + [s.get("location") or "" for s in raw.get("secondaryLocations")
                                                       or []]
                text = " · ".join(n for n in names if n)
                addr = ((raw.get("address") or {}).get("postalAddress") or {})
                structured = normalize_country(addr.get("addressCountry") or "")
                loc = parse_free_location(text, structured if len(structured) == 2 else "")
                country = loc["job_country"]
                wpt = str(raw.get("workplaceType") or "").lower()
                remote = raw.get("isRemote") is True or wpt == "remote"
                work_mode = "remote" if remote else {"hybrid": "hybrid", "onsite": "onsite"}.get(wpt, loc["work_mode"])
                remote_countries = sorted(set(countries_in(text)) | ({country} if country else set())) \
                    if remote else []
                if remote and _US_ONLY(text):
                    remote_countries = ["US"]
                comp = raw.get("compensation") or {}
                jobs.append(JobInput(
                    title=str(raw["title"])[:300], company_name=org, source=self.provider,
                    source_job_id=str(raw["id"]), canonical_url=raw.get("jobUrl") or "",
                    apply_url=raw.get("applyUrl") or "",
                    description=raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml")),
                    posted_date=_iso_date(raw.get("publishedAt")), location_text=text[:300], job_country=country,
                    work_mode=work_mode, remote_eligible_countries=remote_countries,
                    employment_type=str(raw.get("employmentType") or "")[:60],
                    extra={"provider": {"board": org, "department": raw.get("department") or "",
                                        "compensation_text": comp.get("compensationTierSummary") or ""}}))
            except (KeyError, TypeError, ValueError):
                continue
        return _finish(q, jobs, len(data["jobs"]), http)
