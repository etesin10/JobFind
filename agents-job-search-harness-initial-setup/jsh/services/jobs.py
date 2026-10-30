"""JobRepository / triage / application tracking services (Phase 1 core)."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from ..db import Database
from ..models import (
    AIReview, Application, ApplicationEvent, AuditEvent, Company, ContactEvent, DeletedJob, Job, JobLiveness,
    JobModeEvaluation, JobSnapshot, JobSource, JobTag, ResumeVersion, Rule, SearchProfile, Setting, TailoringRun,
    UserJobDecision, utcnow,
)
from ..modes import ACTIVE_PIPELINE_STATUSES, APPLICATION_STATUSES, DISCARD_REASONS, MODE_DEFS, USER_DECISIONS
from ..normalize import (
    canonicalize_url, normalize_country, normalize_country_list, normalize_name, normalize_work_mode,
)
from ..rules import evaluate, ruleset_version
from ..tolerant import parse_date, parse_float

AD_FIELDS = ("company_name", "title", "job_country", "region", "location_text", "remote_eligible_countries",
             "work_mode", "seniority", "employment_type", "salary_min", "salary_max", "currency", "salary_period",
             "canonical_url", "apply_url", "posted_date", "sponsorship_statement", "language", "description")
AD_STATUSES = ("active", "expired", "unknown")
_UNSET = object()
_TERMS = re.compile(r'"([^"]+)"|(\S+)')


class DeletedJobError(ValueError):
    """The vacancy matches one the user deleted permanently (only the user can add it again)."""


def agent_info(job: Job) -> dict[str, Any]:
    """Assessment written by an external agent (job.extra['agent']); {} when absent or malformed."""
    extra = job.extra if isinstance(job.extra, dict) else {}
    info = extra.get("agent")
    return info if isinstance(info, dict) else {}


def _ai(ev: Any) -> dict[str, Any]:
    """Accepted AI review summary of an evaluation ({} when none)."""
    data = getattr(ev, "ai_review", None) if ev is not None else None
    return data if isinstance(data, dict) else {}


def _describe_salary(sal: dict[str, Any]) -> str:
    if not sal or not sal.get("status"):
        return ""
    from .salary import describe  # local import: salary imports models only
    return describe(sal)


def normalize_tag(tag: Any) -> tuple[str, str] | None:
    """(display, normalized) or None. Tags: 1–40 printable chars, inner whitespace collapsed."""
    text = " ".join(str(tag or "").replace(",", " ").split())
    text = "".join(ch for ch in text if ch.isprintable())[:40].strip()
    if not text:
        return None
    return text, fold(text)


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)] if v not in (None, "") else []


def facts_hash(job: Job) -> str:
    """Fingerprint of everything the rules read from the job row (changes ⇒ re-evaluate)."""
    agent = agent_info(job)
    payload = [getattr(job, f) for f in AD_FIELDS] + [job.ad_status, agent.get("score"), agent.get("recommendation")]
    return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode("utf-8")).hexdigest()


def fold(text: str) -> str:
    """Lower-case and strip accents for forgiving search ('analitica' finds 'Analítica')."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def search_terms(search: str) -> list[str]:
    return [fold(a or b) for a, b in _TERMS.findall(search or "") if (a or b).strip()]


class JobInput(BaseModel):
    """Validated input for a job advertisement (manual form, CSV row, connector)."""
    title: str = Field(min_length=1, max_length=300)
    company_name: str = ""
    canonical_url: str = ""
    apply_url: str = ""
    source: str = "manual"
    source_job_id: str = ""
    job_country: str = ""
    region: str = ""
    location_text: str = ""
    remote_eligible_countries: list[str] = Field(default_factory=list)
    work_mode: str = ""
    seniority: str = ""
    employment_type: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str = ""
    salary_period: str = ""
    posted_date: date | None = None
    description: str = ""
    sponsorship_statement: str = ""
    language: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "company_name", "source", "source_job_id", "region", "location_text", "seniority",
                     "employment_type", "salary_period", "description", "sponsorship_statement", "language",
                     mode="before")
    @classmethod
    def _strip(cls, v):
        return "" if v is None else str(v).strip()

    @field_validator("canonical_url", "apply_url", mode="before")
    @classmethod
    def _url(cls, v):
        return canonicalize_url(str(v)) if v else ""

    @field_validator("job_country", mode="before")
    @classmethod
    def _country(cls, v):
        return normalize_country(str(v)) if v else ""

    @field_validator("remote_eligible_countries", mode="before")
    @classmethod
    def _countries(cls, v):
        return normalize_country_list(v)

    @field_validator("work_mode", mode="before")
    @classmethod
    def _work_mode(cls, v):
        return normalize_work_mode(str(v)) if v else ""

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v):
        return str(v).strip().upper() if v else ""

    @field_validator("salary_min", "salary_max", mode="before")
    @classmethod
    def _num(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return float(str(v).replace(",", "").replace("$", "").strip())

    @field_validator("posted_date", mode="before")
    @classmethod
    def _date(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v).strip()[:10])  # ISO date or the date part of an ISO datetime
        except ValueError:
            pass
        parsed = parse_date(v)  # common real-world forms like "October 29, 2026"
        if parsed is None:
            raise ValueError(f"Fecha no reconocida: {str(v)[:40]}")
        return parsed


def jd_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, (date, datetime)) else ("" if v is None else str(v))


class JobService:
    def __init__(self, db: Database):
        self.db = db
        # Set by AppServices: (session, mode_id) -> SkillMatcher | None (approved skills of the mode's candidate)
        self.matcher_factory: Callable[[Session, str], Any] | None = None
        # Set by AppServices: job/row -> normalised salary dict (SalaryService.normalize_job)
        self.salary_normalizer: Callable[[Any], dict[str, Any]] | None = None

    def matcher_for(self, s: Session, mode_id: str):
        return self.matcher_factory(s, mode_id) if self.matcher_factory else None

    # ------------------------------------------------------------------ helpers
    def _audit(self, s: Session, entity_type: str, entity_id: str, action: str, mode_id: str = "",
               actor: str = "user", **details) -> None:
        s.add(AuditEvent(entity_type=entity_type, entity_id=entity_id, action=action, mode_id=mode_id,
                         actor=actor, details=details))

    def _company(self, s: Session, name: str) -> Company | None:
        if not name.strip():
            return None
        key = normalize_name(name)
        comp = s.scalar(select(Company).where(Company.normalized_name == key))
        if comp is None:
            comp = Company(name=name.strip(), normalized_name=key)
            s.add(comp)
            s.flush()
        return comp

    def _find_existing(self, s: Session, inp: JobInput) -> Job | None:
        if inp.source_job_id:
            src = s.scalar(select(JobSource).where(JobSource.provider == inp.source,
                                                   JobSource.source_job_id == inp.source_job_id))
            if src:
                return s.get(Job, src.job_id)
        if inp.canonical_url:
            job = s.scalar(select(Job).where(Job.canonical_url == inp.canonical_url))
            if job:
                return job
            src = s.scalar(select(JobSource).where(JobSource.url == inp.canonical_url))
            if src:
                return s.get(Job, src.job_id)
        if not inp.source_job_id and not inp.canonical_url and inp.company_name:
            # Conservative fallback for URL-less rows: exact normalized company+title+location.
            for job in s.scalars(select(Job).where(Job.title == inp.title)):
                if (normalize_name(job.company_name) == normalize_name(inp.company_name)
                        and normalize_name(job.location_text) == normalize_name(inp.location_text)
                        and job.job_country == inp.job_country):
                    return job
        return None

    def _bump(self, job: Job) -> None:
        job.row_version = (job.row_version or 0) + 1
        job.updated_at = utcnow()

    def _evaluation(self, s: Session, job_id: str, mode_id: str) -> JobModeEvaluation:
        ev = s.scalar(select(JobModeEvaluation).where(JobModeEvaluation.job_id == job_id,
                                                      JobModeEvaluation.mode_id == mode_id))
        if ev is None:
            ev = JobModeEvaluation(job_id=job_id, mode_id=mode_id, mode_data={})
            s.add(ev)
            s.flush()
        return ev

    # ------------------------------------------------------------------ ingest
    @staticmethod
    def _tombstone(s: Session, inp: JobInput) -> DeletedJob | None:
        if inp.source_job_id:
            key = f"{inp.source}|{inp.source_job_id}"
            for tomb in s.scalars(select(DeletedJob)):
                if key in (tomb.source_keys or []):
                    return tomb
        if inp.canonical_url:
            return s.scalar(select(DeletedJob).where(DeletedJob.canonical_url == inp.canonical_url).limit(1))
        return None

    def delete_jobs(self, job_ids: list[str], actor: str = "user") -> dict[str, Any]:
        """PERMANENTLY delete vacancies and everything attached to them (evaluations, decisions, applications,
        JD snapshots, AI reviews, tailoring proposals). A backup is written first. Tailored résumé versions and
        contact interactions are kept (unlinked). A tombstone stops connectors/agents/imports re-creating them."""
        ids = list(dict.fromkeys(j for j in job_ids if j))
        if not ids:
            raise ValueError("Seleccione al menos una vacante.")
        backup = self.db.backup("pre_delete_jobs")
        deleted = []
        with self.db.session() as s:
            for job in list(s.scalars(select(Job).where(Job.id.in_(ids)))):
                srcs = list(s.scalars(select(JobSource).where(JobSource.job_id == job.id)))
                s.add(DeletedJob(job_id=job.id, title=job.title[:300], company_name=job.company_name[:200],
                                 canonical_url=job.canonical_url or "",
                                 source_keys=[f"{x.provider}|{x.source_job_id}" for x in srcs if x.source_job_id]))
                app_ids = list(s.scalars(select(Application.id).where(Application.job_id == job.id)))
                if app_ids:
                    s.execute(delete(ApplicationEvent).where(ApplicationEvent.application_id.in_(app_ids)))
                for model in (Application, JobModeEvaluation, UserJobDecision, AIReview, TailoringRun, JobTag,
                              JobLiveness):
                    s.execute(delete(model).where(model.job_id == job.id))
                s.execute(update(ContactEvent).where(ContactEvent.job_id == job.id).values(job_id=None))
                s.execute(update(ResumeVersion).where(ResumeVersion.job_id == job.id).values(job_id=None))
                self._audit(s, "job", job.id, "job_deleted", "", actor, title=job.title[:200],
                            company=job.company_name[:200], backup=backup.name if backup else "")
                deleted.append(job.title)
                s.delete(job)  # JobSource / JobSnapshot cascade
            s.commit()
        return {"deleted": len(deleted), "titles": deleted, "backup": backup.name if backup else ""}

    def deleted_jobs(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.db.session() as s:
            return [{"id": t.id, "job_id": t.job_id, "title": t.title, "company_name": t.company_name,
                     "canonical_url": t.canonical_url, "deleted_at": _iso(t.deleted_at)}
                    for t in s.scalars(select(DeletedJob).order_by(DeletedJob.deleted_at.desc()).limit(limit))]

    def forget_deleted(self, tombstone_id: str, actor: str = "user") -> None:
        """Allow a deleted vacancy to be imported again (it does not bring the old data back)."""
        with self.db.session() as s:
            tomb = s.get(DeletedJob, tombstone_id)
            if tomb is not None:
                self._audit(s, "job", tomb.job_id, "job_delete_forgotten", "", actor, title=tomb.title[:200])
                s.delete(tomb)
                s.commit()

    def upsert_job(self, inp: JobInput, mode_id: str | list[str] | None = None,
                   actor: str = "user") -> tuple[Job, str]:
        """Idempotent insert/update. Never overwrites user notes, decisions or applications.

        ``mode_id`` may be one mode or a list of modes to tag the job with."""
        tags = [m for m in ([mode_id] if isinstance(mode_id, str) else (mode_id or [])) if m in MODE_DEFS]
        with self.db.session() as s:
            job = self._find_existing(s, inp)
            if job is None:
                tomb = self._tombstone(s, inp)
                if tomb is not None:
                    if actor != "user":
                        raise DeletedJobError(f"La vacante '{tomb.title[:80]}' fue eliminada por el usuario; no se "
                                              "vuelve a importar.")
                    s.delete(tomb)  # the user adds it again by hand: that is an explicit decision
            status = "unchanged"
            if job is None:
                job = Job(title=inp.title)
                s.add(job)
                status = "created"
                changed = list(AD_FIELDS)
                for f in AD_FIELDS:
                    setattr(job, f, getattr(inp, f))
            else:
                changed = []
                for f in AD_FIELDS:
                    new = getattr(inp, f)
                    if new in (None, "", []):
                        continue  # empty incoming values never erase known facts
                    if getattr(job, f) != new:
                        setattr(job, f, list(new) if isinstance(new, list) else new)
                        changed.append(f)
                if changed:
                    status = "updated"
                    self._bump(job)
            comp = self._company(s, inp.company_name)
            if comp is not None:
                job.company_id = comp.id
            if inp.extra:
                job.extra = {**(job.extra if isinstance(job.extra, dict) else {}), **inp.extra}
            current_tags = [t for t in _as_list(job.mode_tags) if t in MODE_DEFS]
            if any(t not in current_tags for t in tags) or current_tags != job.mode_tags:
                job.mode_tags = current_tags + [t for t in tags if t not in current_tags]
            job.last_checked = utcnow()
            s.flush()

            src_key = inp.source_job_id or None
            exists = s.scalar(select(JobSource).where(
                JobSource.job_id == job.id, JobSource.provider == inp.source,
                (JobSource.source_job_id == src_key) if src_key else (JobSource.url == inp.canonical_url)))
            if exists is None:
                s.add(JobSource(job_id=job.id, provider=inp.source, source_job_id=src_key, url=inp.canonical_url))
            if inp.description:
                h = jd_hash(inp.description)
                if s.scalar(select(JobSnapshot).where(JobSnapshot.job_id == job.id, JobSnapshot.jd_hash == h)) is None:
                    s.add(JobSnapshot(job_id=job.id, jd_text=inp.description, jd_hash=h, source_url=inp.canonical_url))
            if status != "unchanged":
                self._audit(s, "job", job.id, f"job_{status}", tags[0] if len(tags) == 1 else "", actor,
                            fields=changed[:30], source=inp.source)
            for mid in MODE_DEFS:
                self._evaluate(s, job, mid)
            s.commit()
            return job, status

    # ------------------------------------------------------------------ evaluation
    def facts(self, job: Job, ev: JobModeEvaluation | None, source: str = "",
              tags: list[str] | None = None) -> dict[str, Any]:
        f = {k: getattr(job, k) for k in AD_FIELDS}
        f["remote_eligible_countries"] = _as_list(f["remote_eligible_countries"])
        agent = agent_info(job)
        f.update(source=source, ad_status=job.ad_status, agent_score=parse_float(agent.get("score")),
                 agent_recommendation=str(agent.get("recommendation") or ""), tags=list(tags or []))
        sal = self.salary_normalizer(job) if self.salary_normalizer else {"status": ""}
        f.update(salary_annual_min=sal.get("min") if sal.get("status") == "ok" else None,
                 salary_annual_max=sal.get("max") if sal.get("status") == "ok" else None)
        if ev is not None:
            f.update(user_decision=ev.user_decision, discard_reason=ev.discard_reason)
            f.update(ev.mode_data or {})
            ai = _ai(ev)
            f.update(ai_fit_score=parse_float(ai.get("technical_fit_score")),
                     ai_priority_score=parse_float(ai.get("user_priority_score")),
                     ai_recommendation=str(ai.get("recommendation") or ""),
                     ai_location_fit=str(ai.get("location_fit") or ""),
                     ai_work_authorization_fit=str(ai.get("work_authorization_fit") or ""))
            detail = ev.skills_detail or {}
            f.update(skills_match_pct=ev.skills_match,
                     matched_skills=[m["name"] for m in detail.get("matched", [])],
                     missing_skills=[m["name"] for m in detail.get("missing", [])])
        return f

    def active_profile_id(self, s: Session, mode_id: str) -> str | None:
        row = s.get(Setting, f"active_profile:{mode_id}")
        if row and row.value:
            prof = s.get(SearchProfile, row.value)
            if prof is not None and prof.mode_id == mode_id:
                return prof.id
        prof = s.scalar(select(SearchProfile).where(SearchProfile.mode_id == mode_id)
                        .order_by(SearchProfile.is_default.desc(), SearchProfile.created_at).limit(1))
        return prof.id if prof else None

    def _rules(self, s: Session, mode_id: str) -> list[Rule]:
        """Rules of the mode's ACTIVE search profile (legacy rules without profile belong to the default)."""
        pid = self.active_profile_id(s, mode_id)
        stmt = select(Rule).where(Rule.mode_id == mode_id)
        if pid:
            prof = s.get(SearchProfile, pid)
            cond = Rule.profile_id == pid
            stmt = stmt.where(or_(cond, Rule.profile_id.is_(None)) if prof.is_default else cond)
        return list(s.scalars(stmt.order_by(Rule.priority, Rule.name)))

    def _evaluate(self, s: Session, job: Job, mode_id: str, rules: list[Rule] | None = None,
                  matcher: Any = _UNSET) -> JobModeEvaluation:
        rules = self._rules(s, mode_id) if rules is None else rules
        if matcher is _UNSET:
            matcher = self.matcher_for(s, mode_id)
        ev = self._evaluation(s, job.id, mode_id)
        ev.skills_match, ev.skills_detail = matcher.match(job.description) if matcher else (None, None)
        src = s.scalar(select(JobSource.provider).where(JobSource.job_id == job.id)) or ""
        tags = list(s.scalars(select(JobTag.tag).where(JobTag.job_id == job.id).order_by(JobTag.normalized)))
        res = evaluate(rules, self.facts(job, ev, src, tags))
        ev.eligibility_status = res.status
        ev.technical_score = res.score
        ev.explanation = [o.to_dict() for o in res.outcomes]
        ev.ruleset_version = ruleset_version(rules)
        ev.evaluated_at = utcnow()
        ev.facts_hash = facts_hash(job)
        return ev

    def reevaluate_mode(self, mode_id: str) -> int:
        with self.db.session() as s:
            rules = self._rules(s, mode_id)
            matcher = self.matcher_for(s, mode_id)
            jobs = list(s.scalars(select(Job)))
            for job in jobs:
                self._evaluate(s, job, mode_id, rules, matcher)
            s.commit()
            return len(jobs)

    # ------------------------------------------------------------------ triage
    def set_decision(self, job_id: str, mode_id: str, decision: str, reason_code: str = "", note: str = "",
                     actor: str = "user") -> None:
        if decision not in USER_DECISIONS:
            raise ValueError(f"Decisión inválida: {decision}")
        if reason_code and reason_code not in DISCARD_REASONS:
            raise ValueError(f"Motivo inválido: {reason_code}")
        with self.db.session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            ev = self._evaluation(s, job_id, mode_id)
            prev = ev.user_decision
            ev.user_decision = decision
            ev.discard_reason = reason_code if decision == "discarded" else ""
            ev.discard_note = note if decision == "discarded" else ""
            s.add(UserJobDecision(job_id=job_id, mode_id=mode_id, decision=decision, previous_decision=prev,
                                  reason_code=reason_code, note=note, actor=actor))
            self._bump(job)
            self._audit(s, "job", job_id, f"decision_{decision}", mode_id, actor, previous=prev,
                        reason=reason_code, note=note)
            s.commit()

    def restore(self, job_id: str, mode_id: str, actor: str = "user") -> str:
        """Undo a discard, returning to the decision held before it (default: new).

        No-op for jobs that are not currently discarded (keeps saved/maybe decisions intact)."""
        with self.db.session() as s:
            ev = s.scalar(select(JobModeEvaluation).where(JobModeEvaluation.job_id == job_id,
                                                          JobModeEvaluation.mode_id == mode_id))
            if ev is None or ev.user_decision != "discarded":
                return ev.user_decision if ev else "new"
            last = s.scalar(select(UserJobDecision).where(
                UserJobDecision.job_id == job_id, UserJobDecision.mode_id == mode_id,
                UserJobDecision.decision == "discarded").order_by(UserJobDecision.created_at.desc()))
        target = (last.previous_decision if last and last.previous_decision not in ("", "discarded") else "new")
        self.set_decision(job_id, mode_id, target, note="Restaurado", actor=actor)
        return target

    def set_override(self, job_id: str, mode_id: str, override: str, reason: str, actor: str = "user") -> None:
        if override not in ("", "include", "exclude"):
            raise ValueError(override)
        if override and not reason.strip():
            raise ValueError("Una anulación manual requiere un motivo.")
        with self.db.session() as s:
            ev = self._evaluation(s, job_id, mode_id)
            ev.override, ev.override_reason = override, reason.strip()
            self._bump(s.get(Job, job_id))
            self._audit(s, "job", job_id, "manual_override", mode_id, actor, override=override, reason=reason)
            s.commit()

    def update_notes(self, job_id: str, notes: str, actor: str = "user") -> None:
        with self.db.session() as s:
            job = s.get(Job, job_id)
            if job.notes != notes:
                job.notes = notes
                self._bump(job)
                self._audit(s, "job", job_id, "notes_updated", "", actor)
            s.commit()

    def toggle_pin(self, job_id: str) -> bool:
        with self.db.session() as s:
            job = s.get(Job, job_id)
            job.pinned = not job.pinned
            s.commit()
            return job.pinned

    # ------------------------------------------------------------------ tags (user-only)
    MAX_TAGS = 20

    def tags(self, job_id: str) -> list[str]:
        with self.db.session() as s:
            return list(s.scalars(select(JobTag.tag).where(JobTag.job_id == job_id).order_by(JobTag.normalized)))

    def all_tags(self) -> list[tuple[str, int]]:
        """(tag, number of jobs) sorted by use, for selectors/autocomplete."""
        with self.db.session() as s:
            rows = s.execute(select(JobTag.tag, JobTag.normalized)).all()
        counts: dict[str, list] = {}
        for tag, norm in rows:
            counts.setdefault(norm, [tag, 0])[1] += 1
        return sorted(((t, n) for t, n in counts.values()), key=lambda x: (-x[1], x[0].lower()))

    def set_tags(self, job_id: str, tags: list[str], actor: str = "user") -> list[str]:
        """Replace the job's tags (normalised, de-duplicated, max 20). Re-evaluates (rules may use tags)."""
        clean: dict[str, str] = {}
        for t in tags or []:
            nt = normalize_tag(t)
            if nt and nt[1] not in clean:
                clean[nt[1]] = nt[0]
        if len(clean) > self.MAX_TAGS:
            raise ValueError(f"Máximo {self.MAX_TAGS} etiquetas por vacante.")
        with self.db.session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            current = {t.normalized: t for t in s.scalars(select(JobTag).where(JobTag.job_id == job_id))}
            if {k: v.tag for k, v in current.items()} == clean:
                return sorted(clean.values(), key=fold)
            for norm, row in current.items():
                if norm not in clean:
                    s.delete(row)
                elif row.tag != clean[norm]:
                    row.tag = clean[norm]
            for norm, tag in clean.items():
                if norm not in current:
                    s.add(JobTag(job_id=job_id, tag=tag, normalized=norm))
            s.flush()
            self._bump(job)
            self._audit(s, "job", job_id, "tags_updated", "", actor, previous=sorted(v.tag for v in current.values()),
                        tags=sorted(clean.values()))
            for mid in MODE_DEFS:
                self._evaluate(s, job, mid)
            s.commit()
        return sorted(clean.values(), key=fold)

    def add_tags(self, job_ids: list[str], tags: list[str], actor: str = "user") -> int:
        n = 0
        for jid in job_ids:
            cur = self.tags(jid)
            if self.set_tags(jid, cur + [t for t in tags if t], actor) != cur:
                n += 1
        return n

    def remove_tags(self, job_ids: list[str], tags: list[str], actor: str = "user") -> int:
        drop = {fold(t) for t in tags}
        n = 0
        for jid in job_ids:
            cur = self.tags(jid)
            keep = [t for t in cur if fold(t) not in drop]
            if keep != cur:
                self.set_tags(jid, keep, actor)
                n += 1
        return n

    def set_ad_status(self, job_id: str, status: str, actor: str = "user") -> bool:
        """active|expired|unknown (e.g. reported by an external agent). Re-evaluates all modes."""
        if status not in AD_STATUSES:
            raise ValueError(f"Estado de anuncio inválido: {status} (opciones: {', '.join(AD_STATUSES)})")
        with self.db.session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.ad_status == status:
                return False
            prev, job.ad_status = job.ad_status, status
            self._bump(job)
            self._audit(s, "job", job_id, "ad_status_changed", "", actor, previous=prev, status=status)
            for mid in MODE_DEFS:
                self._evaluate(s, job, mid)
            s.commit()
            return True

    def update_mode_data(self, job_id: str, mode_id: str, data: dict[str, Any], actor: str = "user") -> None:
        defs = {f.key: f for f in MODE_DEFS[mode_id].mode_fields}
        for k, v in data.items():
            mf = defs.get(k)
            if mf and mf.choices and v not in ("", None) and v not in mf.choices:
                raise ValueError(f"Valor inválido para {k}: {v!r} (opciones: {', '.join(mf.choices)})")
        allowed = set(defs)
        with self.db.session() as s:
            ev = self._evaluation(s, job_id, mode_id)
            new = {**(ev.mode_data or {}), **{k: v for k, v in data.items() if k in allowed}}
            if new != ev.mode_data:
                ev.mode_data = new
                job = s.get(Job, job_id)
                self._bump(job)
                self._audit(s, "job", job_id, "mode_data_updated", mode_id, actor, data=data)
                self._evaluate(s, job, mode_id)
            s.commit()

    # ------------------------------------------------------------------ applications
    def set_application_status(self, job_id: str, mode_id: str, status: str, note: str = "",
                               date_applied: date | None = None, next_followup: date | None = None,
                               actor: str = "user") -> None:
        if status not in APPLICATION_STATUSES:
            raise ValueError(f"Estado inválido: {status}")
        with self.db.session() as s:
            app = s.scalar(select(Application).where(Application.job_id == job_id, Application.mode_id == mode_id))
            if app is None:
                app = Application(job_id=job_id, mode_id=mode_id, status="discovered")
                s.add(app)
                s.flush()
            prev = app.status
            changed = prev != status or (date_applied and app.date_applied != date_applied) or \
                (next_followup and app.next_followup != next_followup)
            if not changed:
                return
            app.status = status
            if status == "applied" and app.date_applied is None and date_applied is None:
                app.date_applied = date.today()
            if date_applied:
                app.date_applied = date_applied
            if next_followup:
                app.next_followup = next_followup
            s.add(ApplicationEvent(application_id=app.id, from_status=prev, to_status=status, note=note, actor=actor))
            self._bump(s.get(Job, job_id))
            self._audit(s, "application", app.id, "status_change", mode_id, actor, job_id=job_id,
                        from_status=prev, to_status=status, note=note)
            s.commit()

    # ------------------------------------------------------------------ queries
    def rows(self, mode_id: str, view: str = "all", search: str = "",
             filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Flat job rows for UI tables and Excel. view: activos|all|discarded|blocked.

        ``search``: every word (or "quoted phrase") must appear in the row fields, notes, tags or JD text;
        case- and accent-insensitive. ``filters``: a saved-view filter dict (see services/views.py)."""
        with self.db.session() as s:
            jobs = list(s.scalars(select(Job)))
            evs = {(e.job_id, e.mode_id): e for e in s.scalars(select(JobModeEvaluation))}
            apps = {(a.job_id, a.mode_id): a for a in s.scalars(select(Application))}
            srcs: dict[str, JobSource] = {}
            for src in s.scalars(select(JobSource).order_by(JobSource.retrieved_at)):
                srcs.setdefault(src.job_id, src)
            snaps: dict[str, tuple[str, Any]] = {}
            for sid, jid, at in s.execute(select(JobSnapshot.id, JobSnapshot.job_id, JobSnapshot.retrieved_at)
                                          .order_by(JobSnapshot.retrieved_at)):
                snaps[jid] = (sid, at)
            tags: dict[str, list[str]] = {}
            for jid, tag in s.execute(select(JobTag.job_id, JobTag.tag).order_by(JobTag.normalized)):
                tags.setdefault(jid, []).append(tag)
            live = {lv.job_id: lv for lv in s.scalars(select(JobLiveness))}
        terms = search_terms(search)
        if filters:
            from .views import matches as filter_matches, validate_filters  # local import: views imports jobs
            filters = validate_filters(filters)
            if filters.q:  # keywords saved in the view add to the typed search
                terms = search_terms(" ".join(x for x in (filters.q, search) if x))
        out = []
        for job in jobs:
            ev = evs.get((job.id, mode_id))
            app = apps.get((job.id, mode_id))
            row = self._row(job, ev, app, srcs.get(job.id), snaps.get(job.id), mode_id, tags.get(job.id, []),
                            live.get(job.id))
            if terms:
                hay = fold(" ".join(str(v) for v in row.values()) + " " + (job.description or ""))
                if not all(t in hay for t in terms):
                    continue
            if view != "all" and not self._in_view(row, view):
                continue
            if filters and not filter_matches(row, filters):
                continue
            out.append(row)
        out.sort(key=lambda r: (not r["pinned"], -(r["technical_score"] or 0), str(r["title"]).lower()))
        return out

    @staticmethod
    def _in_view(row: dict[str, Any], view: str) -> bool:
        discarded = row["user_decision"] == "discarded"
        if view == "discarded":
            return discarded
        if view == "blocked":
            return row["eligibility_status"] == "blocked" and not discarded
        if view == "activos":
            if discarded or row["override"] == "exclude":
                return False
            app_status = row["application_status"]
            if app_status in ACTIVE_PIPELINE_STATUSES:
                return True  # never hide a live process (even if the ad expired / is blocked)
            if APPLICATION_STATUSES.get(app_status, ("", "open"))[1] == "terminal":
                return False
            return row["eligibility_status"] != "blocked" or row["override"] == "include"
        return True

    def _row(self, job: Job, ev, app, src, snap, mode_id: str, tags: list[str] | None = None,
             liveness: Any = None) -> dict[str, Any]:
        agent = agent_info(job)
        sal = self.salary_normalizer(job) if self.salary_normalizer else {"status": "no_salary"}
        flagged = bool(liveness is not None and liveness.status == "likely_closed" and not liveness.dismissed)
        row = {
            "job_id": job.id, "company_id": job.company_id or "", "company_name": job.company_name,
            "title": job.title, "search_mode_tags": ", ".join(_as_list(job.mode_tags)),
            "job_country": job.job_country,
            "region": job.region, "location_text": job.location_text,
            "remote_eligible_countries": ", ".join(_as_list(job.remote_eligible_countries)),
            "work_mode": job.work_mode,
            "seniority": job.seniority, "employment_type": job.employment_type, "salary_min": job.salary_min,
            "salary_max": job.salary_max, "currency": job.currency, "salary_period": job.salary_period,
            "source": src.provider if src else "", "source_job_id": (src.source_job_id or "") if src else "",
            "job_url": job.canonical_url, "apply_url": job.apply_url, "jd_id": snap[0] if snap else "",
            "jd_snapshot_date": _iso(snap[1]) if snap else "", "posted_date": _iso(job.posted_date),
            "last_checked": _iso(job.last_checked), "sponsorship_statement": job.sponsorship_statement,
            "technical_score": ev.technical_score if ev else None,
            "skills_match": ev.skills_match if ev else None,
            "agent_score": parse_float(agent.get("score")),
            "agent_recommendation": str(agent.get("recommendation") or ""),
            "ai_fit_score": parse_float(_ai(ev).get("technical_fit_score")),
            "ai_recommendation": str(_ai(ev).get("recommendation") or ""),
            "eligibility_status": ev.eligibility_status if ev else "unevaluated",
            "user_decision": ev.user_decision if ev else "new", "discard_reason": ev.discard_reason if ev else "",
            "override": ev.override if ev else "", "override_reason": ev.override_reason if ev else "",
            "application_status": app.status if app else "", "date_applied": _iso(app.date_applied) if app else "",
            "next_followup": _iso(app.next_followup) if app else "",
            "contact_id": (app.contact_id or "") if app else "", "resume_version_id": app.resume_version_id if app else "",
            "notes": job.notes, "row_version": job.row_version, "pinned": job.pinned, "ad_status": job.ad_status,
            "tags": ", ".join(tags or []),
            "salary_annual_min": sal.get("min") if sal.get("status") == "ok" else None,
            "salary_annual_max": sal.get("max") if sal.get("status") == "ok" else None,
            "salary_base": sal.get("base", ""), "salary_status": sal.get("status", ""),
            "salary_note": _describe_salary(sal),
            "liveness": ("likely_closed" if flagged else ("dismissed" if liveness is not None and liveness.dismissed
                                                          and liveness.status == "likely_closed"
                                                          else (liveness.status if liveness is not None else ""))),
            "liveness_reason": liveness.reason if liveness is not None else "",
            "liveness_checked": _iso(liveness.checked_at) if liveness is not None else "",
        }
        if mode_id in MODE_DEFS:
            data = (ev.mode_data if ev else {}) or {}
            for mf in MODE_DEFS[mode_id].mode_fields:
                row[mf.key] = data.get(mf.key, "")
        return row

    def detail(self, job_id: str, mode_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            ev = s.scalar(select(JobModeEvaluation).where(JobModeEvaluation.job_id == job_id,
                                                          JobModeEvaluation.mode_id == mode_id))
            app = s.scalar(select(Application).where(Application.job_id == job_id, Application.mode_id == mode_id))
            return {
                "job": job, "evaluation": ev, "application": app,
                "sources": list(s.scalars(select(JobSource).where(JobSource.job_id == job_id))),
                "snapshots": list(s.scalars(select(JobSnapshot).where(JobSnapshot.job_id == job_id)
                                            .order_by(JobSnapshot.retrieved_at.desc()))),
                "decisions": list(s.scalars(select(UserJobDecision).where(UserJobDecision.job_id == job_id)
                                            .order_by(UserJobDecision.created_at.desc()))),
                "app_events": list(s.scalars(select(ApplicationEvent).where(ApplicationEvent.application_id == app.id)
                                             .order_by(ApplicationEvent.created_at.desc()))) if app else [],
            }

    def history(self, mode_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self.db.session() as s:
            stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
            if mode_id and mode_id in MODE_DEFS:
                stmt = stmt.where(AuditEvent.mode_id.in_([mode_id, ""]))
            titles = dict(s.execute(select(Job.id, Job.title)).all())
            return [{"created_at": _iso(e.created_at), "entity_type": e.entity_type, "entity_id": e.entity_id,
                     "job": titles.get(e.entity_id) or titles.get((e.details or {}).get("job_id", ""), ""),
                     "action": e.action, "mode_id": e.mode_id, "actor": e.actor, "details": e.details}
                    for e in s.scalars(stmt)]

    def dashboard(self, mode_id: str) -> dict[str, Any]:
        rows = self.rows(mode_id, "all")
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["eligibility_status"]] = counts.get(r["eligibility_status"], 0) + 1
        apps: dict[str, int] = {}
        for r in rows:
            if r["application_status"]:
                apps[r["application_status"]] = apps.get(r["application_status"], 0) + 1
        today = date.today().isoformat()
        return {
            "total": len(rows),
            "activos": sum(1 for r in rows if self._in_view(r, "activos")),
            "discarded": sum(1 for r in rows if r["user_decision"] == "discarded"),
            "eligibility": counts, "applications": apps,
            "followups_due": [r for r in rows if r["next_followup"] and r["next_followup"] <= today],
        }
