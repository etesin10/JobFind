"""Phase 4 — model-agnostic AI review protocol (spec §7, §8). The app never calls a model.

Flow: ``build_package`` (JSON + Markdown prompt, what is shared is recorded in ``ai_packages``) → the user or an
agent runs ANY model (ChatGPT/Claude web, Claude Code, local LLM…) → ``import_result`` validates the response
against ``ReviewResult`` (schema + echoed ids/hashes) and stores each review as ``received`` → the user
``accept``s or ``reject``s it. Only an accepted review feeds the ``ai_*`` rule fields. Reviews never change
``user_decision`` or application status.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from ..models import AIPackage, AIReview, AuditEvent, Job, JobModeEvaluation, PromptTemplate, utcnow
from ..modes import MODE_DEFS
from ..prompts import MODE_GUIDANCE, REVIEW_PROTOCOL
from ..privacy import mask_data, personal_terms, scrub_list
from ..rules import ruleset_version

MAX_JOBS_PER_PACKAGE = 25
MAX_RESULT_CHARS = 2_000_000
REVIEW_STATUSES = {"received": "Recibido", "accepted": "Aceptado", "rejected": "Rechazado",
                   "superseded": "Reemplazado"}
RECOMMENDATIONS = {"high_priority": "Alta prioridad", "review": "Revisar", "low_priority": "Baja prioridad",
                   "discard_suggested": "Sugiere descartar"}
REQUESTED_TASKS = [
    "summarize responsibilities", "evaluate concrete evidence of fit against the APPROVED skills/work history",
    "location_fit with evidence", "work_authorization_fit (not a legal determination)",
    "job_quality signals and uncertainty", "unknowns and blockers", "questions_for_employer",
    "truthful resume_adaptation_suggestions", "final recommendation",
]
Short = Field(default="", max_length=2000)


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class EvidenceItem(_Model):
    claim: str = Field(min_length=1, max_length=1000)
    jd_quote: str = Field(default="", max_length=1000)
    candidate_skill: str = Field(default="", max_length=200)


class JobQuality(_Model):
    signals: list[str] = Field(default_factory=list, max_length=20)
    uncertainty: str = Short


class JobReview(_Model):
    job_id: str = Field(min_length=1, max_length=64)
    jd_hash: str = Field(default="", max_length=64)
    technical_fit_score: int = Field(ge=0, le=100)
    technical_fit_explanation: str = Field(default="", max_length=4000)
    location_fit: Literal["yes", "no", "unknown"]
    location_evidence: str = Short
    work_authorization_fit: Literal["possible", "blocked", "unknown", "requires_employer_confirmation"]
    work_authorization_evidence: str = Short
    job_quality: JobQuality = Field(default_factory=JobQuality)
    user_priority_score: int | None = Field(default=None, ge=0, le=100)
    recommendation: Literal["high_priority", "review", "low_priority", "discard_suggested"]
    summary: str = Field(default="", max_length=4000)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=30)
    unknowns: list[str] = Field(default_factory=list, max_length=30)
    blockers: list[str] = Field(default_factory=list, max_length=30)
    questions_for_employer: list[str] = Field(default_factory=list, max_length=30)
    resume_adaptation_suggestions: list[str] = Field(default_factory=list, max_length=30)


class ReviewResult(_Model):
    protocol_version: Literal["jsh-review/1"]
    package_id: str = Field(default="", max_length=64)
    mode: Literal["CANADA_VISA", "MEXICO_REMOTE"]
    provider: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=120)
    reviews: list[JobReview] = Field(min_length=1, max_length=200)


def response_schema() -> dict[str, Any]:
    return ReviewResult.model_json_schema()


def _validation_text(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]]
    return "Respuesta inválida — " + "; ".join(parts)


def parse_json_payload(payload: Any, limit: int = MAX_RESULT_CHARS) -> dict[str, Any]:
    """Accept dict, JSON text or JSON wrapped in a ```json fence (models often add one)."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig", "replace")
    if isinstance(payload, str):
        if len(payload) > limit:
            raise ValueError(f"Respuesta demasiado grande (> {limit:,} caracteres)")
        text = payload.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.rsplit("```", 1)[0]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("No se encontró un objeto JSON en la respuesta.")
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"La respuesta no es JSON válido ({exc.msg}, posición {exc.pos}).") from exc
    if not isinstance(payload, dict):
        raise ValueError("La respuesta debe ser un objeto JSON.")
    return payload


def _unknown_keys(model: BaseModel, prefix: str = "") -> list[str]:
    out = [f"{prefix}{k}" for k in (model.model_extra or {})]
    for name, value in model:
        if isinstance(value, BaseModel):
            out += _unknown_keys(value, f"{prefix}{name}.")
        elif isinstance(value, list):
            for i, v in enumerate(value[:3]):
                if isinstance(v, BaseModel):
                    out += _unknown_keys(v, f"{prefix}{name}[{i}].")
    return out


class ReviewService:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    # ------------------------------------------------------------------ shared candidate/rules context
    def candidate_context(self, mode_id: str, include_personal: bool = False) -> dict[str, Any]:
        """APPROVED candidate data only (never proposed/rejected items). Personal header only on opt-in."""
        cand = self.app.candidate.active_candidate(mode_id)
        if cand is None:
            return {"candidate_id": "", "profile_version": None, "approved_skills": [], "experience": [],
                    "note": "No hay perfil de candidato activo para este modo."}
        skills = [sk for sk in self.app.candidate.skills(cand.id, "approved") if sk.active]
        evidence = self.app.candidate.evidence_map(cand.id)
        items = self.app.candidate.items(cand.id, "approved")
        terms = [] if include_personal else personal_terms(cand.contact)

        def ev(sk) -> list[str]:
            raw = [e.excerpt for e in evidence.get(sk.id, [])]
            return (raw if include_personal else scrub_list(raw, terms))[:2]

        def clean(value: Any) -> Any:  # positions/summary text: mask URLs, emails, phones and header values
            return value if include_personal else mask_data(value, terms)
        ctx: dict[str, Any] = {
            "candidate_id": cand.id, "profile_name": cand.name, "profile_version": cand.version,
            "approved_skills": [{"name": sk.name, "category": sk.category, "tag": sk.tag,
                                 "years": sk.years, "proficiency": sk.proficiency, "notes": sk.notes,
                                 "evidence": ev(sk)} for sk in skills],
            "experience": [clean(it.data) for it in items if it.kind == "position"],
            "education": [it.data for it in items if it.kind == "education"],
            "certifications": [it.data for it in items if it.kind == "certification"],
            "languages": [it.data for it in items if it.kind == "language"],
            "summary": [clean(it.data.get("text", "")) for it in items if it.kind == "summary"],
        }
        if include_personal:
            ctx["contact"] = dict(cand.contact or {})
        return ctx

    def rules_context(self, mode_id: str) -> tuple[str, list[dict[str, Any]]]:
        with self.db.session() as s:
            rules = self.app.jobs._rules(s, mode_id)
            return ruleset_version(rules), [
                {"name": r.name, "scope": r.scope, "field": r.field, "operator": r.operator, "value": r.value,
                 "action": r.action, "severity": r.severity, "unknown_handling": r.unknown_handling,
                 "explanation": r.explanation} for r in rules if r.enabled]

    def template(self, key: str) -> PromptTemplate:
        with self.db.session() as s:
            tpl = s.scalar(select(PromptTemplate).where(PromptTemplate.key == key))
            if tpl is None:
                raise KeyError(f"Plantilla no encontrada: {key}")
            return tpl

    def templates(self, kind: str | None = None) -> list[PromptTemplate]:
        with self.db.session() as s:
            stmt = select(PromptTemplate).order_by(PromptTemplate.kind, PromptTemplate.name)
            if kind:
                stmt = stmt.where(PromptTemplate.kind == kind)
            return list(s.scalars(stmt))

    def update_template(self, key: str, body: str, actor: str = "user") -> None:
        body = (body or "").strip()
        if not body:
            raise ValueError("La plantilla no puede quedar vacía.")
        with self.db.session() as s:
            tpl = s.scalar(select(PromptTemplate).where(PromptTemplate.key == key))
            if tpl is None:
                raise KeyError(key)
            if tpl.body != body:
                tpl.body, tpl.version, tpl.updated_at = body[:50_000], tpl.version + 1, utcnow()
                s.add(AuditEvent(entity_type="prompt_template", entity_id=tpl.id, action="template_updated",
                                 actor=actor, details={"key": key, "version": tpl.version}))
                s.commit()

    def reset_template(self, key: str, actor: str = "user") -> None:
        from ..prompts import DEFAULT_TEMPLATES
        default = next((t for t in DEFAULT_TEMPLATES if t["key"] == key), None)
        if default is None:
            raise KeyError(key)
        self.update_template(key, default["body"], actor)

    # ------------------------------------------------------------------ export
    def build_package(self, mode_id: str, job_ids: list[str], include_personal: bool = False,
                      template_key: str = "review_default", actor: str = "user") -> dict[str, Any]:
        if mode_id not in MODE_DEFS:
            raise ValueError(f"Modo inválido: {mode_id}")
        job_ids = list(dict.fromkeys(j for j in job_ids if j))
        if not job_ids:
            raise ValueError("Seleccione al menos una vacante.")
        if len(job_ids) > MAX_JOBS_PER_PACKAGE:
            raise ValueError(f"Máximo {MAX_JOBS_PER_PACKAGE} vacantes por paquete (límite de contexto de los modelos).")
        tpl = self.template(template_key)
        ruleset, rules = self.rules_context(mode_id)
        candidate = self.candidate_context(mode_id, include_personal)
        jobs = []
        for jid in job_ids:
            doc = self.app.jd.document(jid, mode_id)
            doc["job"].pop("agent", None)  # do not bias the reviewer with another agent's opinion
            jobs.append({"job_id": jid, "jd_hash": doc["jd"]["hash"], "job": doc["job"],
                         "jd": {k: doc["jd"][k] for k in ("flat_text", "keywords", "years_experience", "education",
                                                          "work_authorization", "work_mode_hints",
                                                          "salary_mentions")},
                         "deterministic_evaluation": {k: v for k, v in (doc.get("evaluation") or {}).items()
                                                      if k in ("eligibility_status", "technical_score",
                                                               "skills_match", "explanation", "mode_data")},
                         "skills_match": doc.get("match")})
        with self.db.session() as s:
            pkg = AIPackage(kind="review", mode_id=mode_id, job_ids=job_ids,
                            jd_hashes={j["job_id"]: j["jd_hash"] for j in jobs},
                            candidate_id=candidate.get("candidate_id", ""),
                            profile_version=candidate.get("profile_version"), ruleset_version=ruleset,
                            template_key=tpl.key, template_version=tpl.version, include_personal=include_personal,
                            actor=actor[:60])
            s.add(pkg)
            s.flush()
            s.add(AuditEvent(entity_type="ai_package", entity_id=pkg.id, action="package_exported", mode_id=mode_id,
                             actor=actor[:60], details={"kind": "review", "jobs": len(job_ids),
                                                        "include_personal": include_personal}))
            s.commit()
            package_id, created = pkg.id, pkg.created_at
        return {
            "protocol_version": REVIEW_PROTOCOL, "package_id": package_id, "kind": "review", "mode": mode_id,
            "mode_name": MODE_DEFS[mode_id].name, "created_at": created.isoformat(timespec="seconds"),
            "instructions": tpl.body + "\n\n" + MODE_GUIDANCE.get(mode_id, ""),
            "template": {"key": tpl.key, "version": tpl.version},
            "requested_tasks": REQUESTED_TASKS, "ruleset_version": ruleset, "rules": rules,
            "candidate": candidate, "job_ids": job_ids, "jobs": jobs,
            "response_schema": response_schema(),
            "response_example": {"protocol_version": REVIEW_PROTOCOL, "package_id": package_id, "mode": mode_id,
                                 "provider": "<chatgpt|claude|local|…>", "model": "<model name>",
                                 "reviews": [{"job_id": job_ids[0], "jd_hash": jobs[0]["jd_hash"],
                                              "technical_fit_score": 70, "technical_fit_explanation": "…",
                                              "location_fit": "unknown", "work_authorization_fit": "unknown",
                                              "recommendation": "review", "summary": "…", "evidence": [],
                                              "unknowns": [], "blockers": [], "questions_for_employer": [],
                                              "resume_adaptation_suggestions": []}]},
        }

    @staticmethod
    def package_markdown(pkg: dict[str, Any]) -> str:
        """A single paste-able prompt for web chats (ChatGPT/Claude): instructions + the package as JSON."""
        data = {k: v for k, v in pkg.items() if k not in ("instructions",)}
        return (f"# Job Search Harness — paquete de {'revisión' if pkg['kind'] == 'review' else 'adaptación de CV'}\n\n"
                f"{pkg['instructions']}\n\n## Package (JSON)\n\n```json\n"
                f"{json.dumps(data, ensure_ascii=False, indent=1, default=str)}\n```\n")

    def export_files(self, pkg: dict[str, Any]) -> dict[str, Any]:
        """Write <exports>/<kind>_<mode>_<stamp>_<id>.json + .md (paths relative to the portable home)."""
        exports = self.app.paths.exports
        exports.mkdir(parents=True, exist_ok=True)
        stem = f"{pkg['kind']}_{pkg['mode']}_{datetime.now():%Y%m%d_%H%M%S}_{pkg['package_id'][:8]}"
        jpath, mpath = exports / f"{stem}.json", exports / f"{stem}.md"
        jpath.write_text(json.dumps(pkg, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        mpath.write_text(self.package_markdown(pkg), encoding="utf-8")
        return {"json": jpath, "markdown": mpath}

    # ------------------------------------------------------------------ import
    def import_result(self, payload: Any, source: str = "file", actor: str = "user") -> dict[str, Any]:
        """Validate and store a review result. Raises ValueError for file-level problems; per-review problems
        are reported in ``rejected``."""
        data = parse_json_payload(payload)
        if data.get("protocol_version") != REVIEW_PROTOCOL:
            raise ValueError(f"protocol_version debe ser '{REVIEW_PROTOCOL}' (recibido: "
                             f"{str(data.get('protocol_version'))[:40]!r}).")
        try:
            result = ReviewResult.model_validate(data)
        except ValidationError as exc:
            raise ValueError(_validation_text(exc)) from exc
        warnings = [f"Campos no reconocidos ignorados: {', '.join(_unknown_keys(result))[:300]}"] \
            if _unknown_keys(result) else []
        report: dict[str, Any] = {"received": [], "rejected": [], "warnings": warnings}
        with self.db.session() as s:
            pkg = None
            if result.package_id:
                pkg = s.get(AIPackage, result.package_id)
                if pkg is None or pkg.kind != "review":
                    raise ValueError("package_id desconocido: la respuesta no corresponde a un paquete exportado "
                                     "por esta app.")
                if pkg.mode_id != result.mode:
                    raise ValueError(f"El paquete es del modo {pkg.mode_id} y la respuesta dice {result.mode}.")
            seen: set[str] = set()
            for rv in result.reviews:
                reason = ""
                job = s.get(Job, rv.job_id)
                if rv.job_id in seen:
                    reason = "job_id repetido en la respuesta"
                elif job is None:
                    reason = "job_id no existe"
                elif pkg is not None and rv.job_id not in (pkg.job_ids or []):
                    reason = "job_id no pertenece al paquete"
                elif pkg is not None and rv.jd_hash != (pkg.jd_hashes or {}).get(rv.job_id):
                    reason = "jd_hash no coincide con el paquete (el modelo no copió el hash exacto)"
                if reason:
                    report["rejected"].append({"job_id": rv.job_id, "reason": reason})
                    continue
                seen.add(rv.job_id)
                current = self.app.jd.document(rv.job_id, None, include_text=False)["jd"]["hash"]
                stale = bool(rv.jd_hash) and rv.jd_hash != current
                notes = []
                if not rv.jd_hash:
                    notes.append("sin jd_hash: no se puede comprobar que se analizó la JD actual")
                if stale:
                    notes.append("la JD cambió desde el análisis (revisión desactualizada)")
                review = AIReview(job_id=rv.job_id, mode_id=result.mode, package_id=result.package_id,
                                  jd_hash=rv.jd_hash, stale=stale, provider=result.provider, model=result.model,
                                  source=source[:20], status="received",
                                  result=rv.model_dump(mode="json"), warnings=notes, actor=actor[:60])
                s.add(review)
                s.flush()
                s.add(AuditEvent(entity_type="job", entity_id=rv.job_id, action="ai_review_received",
                                 mode_id=result.mode, actor=actor[:60],
                                 details={"review_id": review.id, "recommendation": rv.recommendation,
                                          "score": rv.technical_fit_score, "provider": result.provider,
                                          "model": result.model, "stale": stale}))
                report["received"].append({"review_id": review.id, "job_id": rv.job_id, "stale": stale,
                                           "warnings": notes})
            s.commit()
        return report

    # ------------------------------------------------------------------ decisions
    def list(self, mode_id: str | None = None, status: str | None = None, job_id: str | None = None,
             limit: int = 500) -> list[dict[str, Any]]:
        with self.db.session() as s:
            stmt = select(AIReview, Job.title, Job.company_name).join(Job, Job.id == AIReview.job_id)
            if mode_id:
                stmt = stmt.where(AIReview.mode_id == mode_id)
            if status:
                stmt = stmt.where(AIReview.status == status)
            if job_id:
                stmt = stmt.where(AIReview.job_id == job_id)
            rows = s.execute(stmt.order_by(AIReview.created_at.desc()).limit(limit)).all()
        return [self._dict(r, title, company) for r, title, company in rows]

    @staticmethod
    def _dict(r: AIReview, title: str = "", company: str = "") -> dict[str, Any]:
        res = r.result or {}
        return {"review_id": r.id, "job_id": r.job_id, "title": title, "company_name": company, "mode_id": r.mode_id,
                "status": r.status, "stale": r.stale, "provider": r.provider, "model": r.model, "source": r.source,
                "package_id": r.package_id, "technical_fit_score": res.get("technical_fit_score"),
                "recommendation": res.get("recommendation", ""), "location_fit": res.get("location_fit", ""),
                "work_authorization_fit": res.get("work_authorization_fit", ""), "summary": res.get("summary", ""),
                "result": res, "warnings": list(r.warnings or []), "actor": r.actor,
                "created_at": r.created_at.isoformat(timespec="minutes"),
                "decided_at": r.decided_at.isoformat(timespec="minutes") if r.decided_at else ""}

    def get(self, review_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            r = s.get(AIReview, review_id)
            if r is None:
                raise KeyError(review_id)
            job = s.get(Job, r.job_id)
            return self._dict(r, job.title if job else "", job.company_name if job else "")

    def accept(self, review_id: str, actor: str = "user") -> None:
        """Make this the active AI review of the job+mode (feeds ai_* rule fields). Never changes decisions."""
        with self.db.session() as s:
            r = s.get(AIReview, review_id)
            if r is None:
                raise KeyError(review_id)
            if r.status == "accepted":
                return
            for other in s.scalars(select(AIReview).where(AIReview.job_id == r.job_id, AIReview.mode_id == r.mode_id,
                                                          AIReview.status == "accepted")):
                other.status = "superseded"
            r.status, r.decided_by, r.decided_at = "accepted", actor[:60], utcnow()
            ev = self.app.jobs._evaluation(s, r.job_id, r.mode_id)
            res = r.result or {}
            ev.ai_review = {"review_id": r.id, "technical_fit_score": res.get("technical_fit_score"),
                            "recommendation": res.get("recommendation"), "location_fit": res.get("location_fit"),
                            "work_authorization_fit": res.get("work_authorization_fit"),
                            "user_priority_score": res.get("user_priority_score"), "summary": res.get("summary", ""),
                            "provider": r.provider, "model": r.model, "stale": r.stale,
                            "accepted_at": r.decided_at.isoformat(timespec="seconds")}
            s.add(AuditEvent(entity_type="job", entity_id=r.job_id, action="ai_review_accepted", mode_id=r.mode_id,
                             actor=actor[:60], details={"review_id": r.id}))
            job = s.get(Job, r.job_id)
            self.app.jobs._bump(job)
            self.app.jobs._evaluate(s, job, r.mode_id)
            s.commit()

    def reject(self, review_id: str, actor: str = "user") -> None:
        with self.db.session() as s:
            r = s.get(AIReview, review_id)
            if r is None:
                raise KeyError(review_id)
            was_active = r.status == "accepted"
            r.status, r.decided_by, r.decided_at = "rejected", actor[:60], utcnow()
            s.add(AuditEvent(entity_type="job", entity_id=r.job_id, action="ai_review_rejected", mode_id=r.mode_id,
                             actor=actor[:60], details={"review_id": r.id}))
            if was_active:
                ev = self.app.jobs._evaluation(s, r.job_id, r.mode_id)
                ev.ai_review = None
                job = s.get(Job, r.job_id)
                self.app.jobs._bump(job)
                self.app.jobs._evaluate(s, job, r.mode_id)
            s.commit()

    def counts(self, mode_id: str | None = None) -> dict[str, int]:
        with self.db.session() as s:
            stmt = select(AIReview.status, func.count()).group_by(AIReview.status)
            if mode_id:
                stmt = stmt.where(AIReview.mode_id == mode_id)
            got = dict(s.execute(stmt).all())
        return {k: got.get(k, 0) for k in REVIEW_STATUSES}

    def evaluation_of(self, job_id: str, mode_id: str) -> JobModeEvaluation | None:
        with self.db.session() as s:
            return s.scalar(select(JobModeEvaluation).where(JobModeEvaluation.job_id == job_id,
                                                            JobModeEvaluation.mode_id == mode_id))
