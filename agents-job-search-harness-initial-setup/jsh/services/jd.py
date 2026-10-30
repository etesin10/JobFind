"""One canonical, JSON-able "job document" for humans, exports, the REST API and MCP tools."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from ..candidate.vocabulary import BY_KEY
from ..jd import analyze_jd, importance_of, to_markdown
from ..models import Job, JobModeEvaluation, JobSource
from ..modes import MODE_DEFS
from ..privacy import personal_terms, scrub
from .jobs import _as_list, _iso, agent_info

PUBLIC_EVAL_KEYS = ("eligibility_status", "technical_score", "skills_match", "user_decision", "discard_reason",
                    "override", "ruleset_version")


def job_facts(job: Job, source: str = "") -> dict[str, Any]:
    return {
        "job_id": job.id, "title": job.title, "company_name": job.company_name, "company_id": job.company_id or "",
        "job_country": job.job_country, "region": job.region, "location_text": job.location_text,
        "remote_eligible_countries": _as_list(job.remote_eligible_countries), "work_mode": job.work_mode,
        "seniority": job.seniority, "employment_type": job.employment_type, "salary_min": job.salary_min,
        "salary_max": job.salary_max, "currency": job.currency, "salary_period": job.salary_period,
        "canonical_url": job.canonical_url, "apply_url": job.apply_url, "posted_date": _iso(job.posted_date),
        "last_checked": _iso(job.last_checked), "ad_status": job.ad_status,
        "sponsorship_statement": job.sponsorship_statement, "language": job.language,
        "mode_tags": _as_list(job.mode_tags), "source": source, "row_version": job.row_version,
        "updated_at": _iso(job.updated_at), "agent": agent_info(job),
    }


class JDService:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    def document(self, job_id: str, mode_id: str | None = None, include_text: bool = True) -> dict[str, Any]:
        """Facts + deterministic JD analysis + evaluation + CV match (with required/preferred importance)."""
        mode_id = mode_id if mode_id in MODE_DEFS else None
        with self.db.session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            src = s.scalar(select(JobSource.provider).where(JobSource.job_id == job_id)
                           .order_by(JobSource.retrieved_at)) or ""
            evs = {e.mode_id: e for e in s.scalars(select(JobModeEvaluation)
                                                    .where(JobModeEvaluation.job_id == job_id))}
            facts = job_facts(job, src)
            description = job.description or ""
        jd = analyze_jd(description)
        if not include_text:
            jd.pop("flat_text", None)
            for sec in jd["sections"]:
                sec["text"] = sec["text"][:300]
        doc: dict[str, Any] = {"job": facts, "jd": jd, "mode_id": mode_id or ""}
        doc["evaluations"] = {m: {k: getattr(e, k) for k in PUBLIC_EVAL_KEYS} for m, e in evs.items()}
        ev = evs.get(mode_id) if mode_id else None
        if ev is not None:
            doc["evaluation"] = {**doc["evaluations"][mode_id],
                                 "explanation": [{k: o.get(k) for k in ("name", "effect", "explanation", "field")}
                                                 for o in (ev.explanation or [])],
                                 "mode_data": dict(ev.mode_data or {}), "ai_review": ev.ai_review or None}
            detail = ev.skills_detail or {}
            if detail:
                cand = self.app.candidate.active_candidate(mode_id)
                terms = personal_terms(cand.contact if cand else None)
                doc["match"] = {"pct": ev.skills_match, "candidate_name": detail.get("candidate_name", ""),
                                "insufficient": bool(detail.get("insufficient")),
                                "matched": [self._with_importance(jd, m, terms) for m in detail.get("matched", [])],
                                "missing": [self._with_importance(jd, m, terms) for m in detail.get("missing", [])]}
        return doc

    @staticmethod
    def _with_importance(jd: dict[str, Any], m: dict[str, Any], terms: list[str]) -> dict[str, Any]:
        entry = BY_KEY.get(m["name"])
        phrases = [m["name"], *(entry.aliases if entry else ())]
        return {"name": m["name"], "status": m.get("status", "approved"), "kind": m.get("kind", ""),
                "importance": importance_of(jd, phrases), "jd_evidence": m.get("jd", ""),
                "cv_evidence": scrub(m.get("cv", ""), terms)}  # CV header lines never leave the app

    def markdown(self, job_id: str, mode_id: str | None = None) -> str:
        return to_markdown(self.document(job_id, mode_id))

    def json_text(self, job_id: str, mode_id: str | None = None) -> str:
        return json.dumps(self.document(job_id, mode_id), ensure_ascii=False, indent=2, default=str)
