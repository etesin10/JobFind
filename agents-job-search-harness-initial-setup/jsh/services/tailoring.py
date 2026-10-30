"""Phase 5 — truthful, per-job résumé tailoring (spec §3.5-9, §8 'Résumé analysis request').

- The approved MASTER résumé is addressed with stable ids: ``S1`` (summary), ``P1`` (position), ``P1.H2`` (bullet),
  ``skill:<name>``, ``E1`` / ``C1`` / ``L1`` (education / certification / language).
- A proposal (deterministic, or returned by any external AI via file / REST / MCP) is a list of changes:
  ``rewrite`` (bullet or summary), ``reorder`` (bullets of a position, or skills), ``remove``, ``add`` (new bullet,
  always flagged). Titles, employers and dates can never be targeted.
- Automatic truth checks flag new numbers, skills that are not approved and changes without evidence. Flagged
  changes need an explicit "verified" confirmation from the user.
- Approval creates ``ResumeVersion(kind="tailored")`` with full provenance; ``.docx`` is rendered locally.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from ..candidate.vocabulary import BY_KEY, find_vocab, fold
from ..models import (
    AIPackage, Application, AuditEvent, CandidateProfile, Job, ResumeVersion, TailoringRun, utcnow,
)
from ..modes import MODE_DEFS
from ..prompts import TAILORING_PROTOCOL
from ..privacy import mask_data, mask_document, personal_terms, scrub_list
from .reviews import _unknown_keys, _validation_text, parse_json_payload

ACTIONS = {"rewrite": "Reescribir", "reorder": "Reordenar", "remove": "Quitar", "add": "Agregar (nuevo)"}
_NUM = re.compile(r"(?<![\w])[$€£]?\d[\d,.]*\s?(?:%|k|m|x|\+)?", re.I)
_WORD = re.compile(r"[A-Za-zÀ-ÿ][\w&+#'’-]*")
# Words that introduce facts (amounts, dates, seniority) when they appear in a rewrite but not in the evidence.
_FACT_WORDS = set("""one two three four five six seven eight nine ten eleven twelve thirteen fifteen twenty thirty forty
fifty sixty seventy eighty ninety hundred hundreds thousand thousands million millions billion billions percent
percentage dozen dozens double doubled triple tripled quadrupled half twice
uno dos tres cuatro cinco seis siete ocho nueve diez doce quince veinte treinta cuarenta cincuenta cien ciento
cientos mil miles millon millones porciento doble triple mitad
january february march april may june july august september october november december
enero febrero marzo abril mayo junio julio agosto septiembre setiembre octubre noviembre diciembre
director directors manager managers lead leader head vp vice president chief cto ceo cfo coo cio principal staff
architect supervisor owner founder cofounder co-founder partner executive
gerente jefe jefa lider coordinador coordinadora directora presidente fundador socio""".split())
MAX_TEXT = 600
SECTION_TITLES = {
    "en": {"summary": "Professional Summary", "experience": "Experience", "skills": "Skills",
           "education": "Education", "certifications": "Certifications", "languages": "Languages"},
    "es": {"summary": "Resumen profesional", "experience": "Experiencia", "skills": "Habilidades",
           "education": "Educación", "certifications": "Certificaciones", "languages": "Idiomas"},
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class Change(_Model):
    change_id: str = Field(min_length=1, max_length=40)
    action: Literal["rewrite", "reorder", "remove", "add"]
    target_id: str = Field(min_length=1, max_length=120)
    original: str = Field(default="", max_length=4000)
    proposed: str = Field(default="", max_length=4000)
    order: list[str] = Field(default_factory=list, max_length=200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    rationale: str = Field(default="", max_length=2000)
    keywords: list[str] = Field(default_factory=list, max_length=30)


class TailoringProposal(_Model):
    protocol_version: Literal["jsh-tailoring/1"]
    package_id: str = Field(default="", max_length=64)
    job_id: str = Field(min_length=1, max_length=64)
    jd_hash: str = Field(default="", max_length=64)
    master_version_id: str = Field(min_length=1, max_length=64)
    provider: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=120)
    changes: list[Change] = Field(default_factory=list, max_length=80)
    skills_order: list[str] = Field(default_factory=list, max_length=300)
    missing_requirements: list[str] = Field(default_factory=list, max_length=60)
    keywords_used: list[str] = Field(default_factory=list, max_length=60)
    questions_for_user: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=4000)


def response_schema() -> dict[str, Any]:
    return TailoringProposal.model_json_schema()


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[\s,]", "", m.group(0)).lower().rstrip(".") for m in _NUM.finditer(text or "")}


def new_fact_terms(proposed: str, support: str, allowed: set[str]) -> list[str]:
    """Words in ``proposed`` that may introduce a NEW fact: proper nouns (employers, clients, products),
    titles, months and spelled-out numbers absent from the evidence. ``allowed`` = folded approved skill words."""
    known = {fold(w) for w in _WORD.findall(support or "")} | allowed
    out: list[str] = []
    sentence_start = True
    for m in re.finditer(r"[.!?;:]\s+|" + _WORD.pattern, proposed or ""):
        tok = m.group(0)
        if not _WORD.fullmatch(tok):
            sentence_start = True
            continue
        low = fold(tok).strip("'’")
        first, sentence_start = sentence_start, False
        if low in known or len(low) < 2:
            continue
        if low in _FACT_WORDS or (tok[0].isupper() and not first) or (tok.isupper() and len(tok) > 1):
            if tok not in out:
                out.append(tok)
    return out


def _norm(text: str) -> str:
    return " ".join(fold(text or "").split())


def resume_doc(content: dict[str, Any], master_version_id: str) -> dict[str, Any]:
    """Master ResumeVersion.content → addressable résumé document."""
    items = content.get("items", [])
    doc: dict[str, Any] = {"master_version_id": master_version_id, "candidate": content.get("candidate", ""),
                           "summary": [], "experience": [], "skills": [], "education": [], "certifications": [],
                           "languages": []}
    counters = {"summary": 0, "position": 0, "education": 0, "certification": 0, "language": 0}
    for it in items:
        kind, data = it.get("kind"), it.get("data") or {}
        if kind not in counters:
            continue
        counters[kind] += 1
        n = counters[kind]
        if kind == "summary" and data.get("text"):
            doc["summary"].append({"id": f"S{n}", "text": data["text"]})
        elif kind == "position":
            bullets = []
            for j, h in enumerate(data.get("highlights") or [], 1):
                text = h.get("text") if isinstance(h, dict) else str(h)
                if text:
                    bullets.append({"id": f"P{n}.H{j}", "text": text})
            dates = data.get("dates_text") or " – ".join(x for x in (data.get("start"), data.get("end")
                                                                       or data.get("end_note")) if x)
            doc["experience"].append({"id": f"P{n}", "title": data.get("title", ""),
                                      "company": data.get("company", ""), "location": data.get("location", ""),
                                      "dates": dates, "bullets": bullets})
        elif kind == "education":
            doc["education"].append({"id": f"E{n}", **{k: data.get(k, "") for k in ("degree", "institution", "year")}})
        elif kind == "certification":
            doc["certifications"].append({"id": f"C{n}", **{k: data.get(k, "") for k in ("name", "issuer", "year")}})
        elif kind == "language":
            doc["languages"].append({"id": f"L{n}", "language": data.get("language", ""),
                                     "level": data.get("level", "") or data.get("as_written", "")})
    for sk in content.get("skills", []):
        doc["skills"].append({"id": f"skill:{sk['name']}", "name": sk["name"], "category": sk.get("category", ""),
                              "evidence": sk.get("evidence", [])[:2]})
    return doc


def element_texts(doc: dict[str, Any]) -> dict[str, str]:
    """id → text, for evidence lookups."""
    out = {s["id"]: s["text"] for s in doc["summary"]}
    for p in doc["experience"]:
        out[p["id"]] = f"{p['title']} {p['company']} {p['dates']}"
        out.update({b["id"]: b["text"] for b in p["bullets"]})
    for sk in doc["skills"]:
        out[sk["id"]] = " ".join([sk["name"], *sk.get("evidence", [])])
    for key in ("education", "certifications", "languages"):
        for e in doc[key]:
            out[e["id"]] = " ".join(str(v) for k, v in e.items() if k != "id")
    return out


def render_text(doc: dict[str, Any], header: dict[str, Any] | None = None, lang: str = "en") -> str:
    t = SECTION_TITLES.get(lang, SECTION_TITLES["en"])
    lines: list[str] = []
    header = header or {}
    if header.get("name"):
        lines.append(header["name"])
    if header.get("headline"):
        lines.append(header["headline"])
    contact = " | ".join(x for x in (header.get("email"), header.get("phone"), header.get("location"),
                                     *(header.get("links") or [])) if x)
    if contact:
        lines.append(contact)
    if doc["summary"]:
        lines += ["", t["summary"].upper()] + [s["text"] for s in doc["summary"]]
    if doc["experience"]:
        lines += ["", t["experience"].upper()]
        for p in doc["experience"]:
            lines.append(" — ".join(x for x in (p["title"], p["company"], p["location"], p["dates"]) if x))
            lines += [f"• {b['text']}" for b in p["bullets"]]
    if doc["skills"]:
        lines += ["", t["skills"].upper(), ", ".join(s["name"] for s in doc["skills"])]
    for key in ("education", "certifications", "languages"):
        if doc[key]:
            lines += ["", t[key].upper()]
            lines += [", ".join(str(v) for k, v in e.items() if k != "id" and v) for e in doc[key]]
    return "\n".join(lines).strip() + "\n"


class TailoringService:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    # ------------------------------------------------------------------ master
    def master(self, mode_id: str, create: bool = True) -> tuple[CandidateProfile, ResumeVersion]:
        """Active candidate of the mode + its latest approved master version (saved automatically from the
        APPROVED profile when none exists yet)."""
        cand = self.app.candidate.active_candidate(mode_id)
        if cand is None:
            raise ValueError("No hay perfil de candidato activo para este modo (Skills y CV).")
        with self.db.session() as s:
            ver = s.scalar(select(ResumeVersion).where(ResumeVersion.candidate_id == cand.id,
                                                       ResumeVersion.kind == "master", ResumeVersion.approved.is_(True))
                           .order_by(ResumeVersion.version.desc()).limit(1))
        if ver is None:
            if not create:
                raise ValueError("No hay versión maestra del CV.")
            ver = self.app.candidate.save_master_version(cand.id, actor="tailoring")
        return cand, ver

    def master_doc(self, mode_id: str) -> dict[str, Any]:
        _, ver = self.master(mode_id)
        return resume_doc(ver.content or {}, ver.id)

    # ------------------------------------------------------------------ résumé as a source for AI agents
    def resume_for_agents(self, mode_id: str, include_source_text: bool = True) -> dict[str, Any]:
        """Everything an AI needs to read or build a tailored résumé, WITHOUT personal contact data: the master
        résumé with element ids, approved/proposed skill names, the tailored versions and (optionally) the imported
        CV texts with the contact header removed."""
        if mode_id not in MODE_DEFS:
            raise ValueError(f"Modo inválido: {mode_id}")
        cand, ver = self.master(mode_id)
        terms = personal_terms(cand.contact)
        doc = resume_doc(ver.content or {}, ver.id)
        resume = {**mask_data({k: v for k, v in doc.items() if k != "skills"}, terms), "master_version_id": ver.id,
                  "skills": [{**s, "evidence": scrub_list(s.get("evidence", []), terms)} for s in doc["skills"]]}
        skills = self.app.candidate.skills(cand.id)
        out: dict[str, Any] = {
            "candidate_id": cand.id, "profile_name": cand.name, "profile_version": cand.version,
            "master_version_id": ver.id, "master_resume": resume,
            "approved_skills": [s.name for s in skills if s.status == "approved" and s.active],
            "proposed_skills": [s.name for s in skills if s.status == "proposed"],
            "rejected_skills": [s.name for s in skills if s.status == "rejected"],
            "tailored_versions": [self._version_summary(v) for v in self.tailored_versions()
                                  if v.candidate_id == cand.id],
            "how_to": "Tailor for a job: get_tailoring_package / POST /packages/tailoring, then submit a "
                      "jsh-tailoring/1 proposal. Suggest missing skills with propose_skills (the user approves them).",
        }
        if include_source_text:
            out["source_texts"] = [{"source_id": src.id, "file_name": src.file_name, "kind": src.kind,
                                    "imported_at": src.imported_at.isoformat(timespec="seconds"),
                                    "text": mask_document(src.text, terms)[:60_000]}
                                   for src in self.app.candidate.sources(cand.id)]
        return out

    def propose_skills(self, mode_id: str, skills: list[dict[str, Any]], actor: str) -> dict[str, Any]:
        """AI-suggested skills for the mode's active candidate → always PROPOSED for the user's review."""
        cand = self.app.candidate.active_candidate(mode_id) if mode_id in MODE_DEFS else None
        if cand is None:
            raise ValueError("No hay perfil de candidato activo para este modo.")
        return self.app.candidate.propose_skills(cand.id, skills, actor)

    @staticmethod
    def _version_summary(v: ResumeVersion) -> dict[str, Any]:
        prov = v.provenance or {}
        return {"resume_version_id": v.id, "name": v.name, "version": v.version, "job_id": v.job_id or "",
                "approved_at": v.approved_at.isoformat(timespec="seconds") if v.approved_at else "",
                "source": prov.get("source", ""), "provider": prov.get("provider", ""),
                "docx": bool(prov.get("docx"))}

    def tailored_for_agents(self, version_id: str) -> dict[str, Any]:
        """One approved tailored résumé (structured + plain text), personal header masked."""
        with self.db.session() as s:
            ver = s.get(ResumeVersion, version_id)
            if ver is None or ver.kind != "tailored":
                raise KeyError(version_id)
            cand = s.get(CandidateProfile, ver.candidate_id)
            terms = personal_terms(cand.contact if cand else None)
            content = mask_data(ver.content or {}, terms)
            summary = self._version_summary(ver)
            approved_ids = list((ver.provenance or {}).get("approved_change_ids", []))
        return {**summary, "resume": content, "text": render_text(content, None, "en"),
                "approved_change_ids": approved_ids}

    # ------------------------------------------------------------------ deterministic proposal
    def deterministic_proposal(self, job_id: str, mode_id: str, actor: str = "user") -> dict[str, Any]:
        """Reorder-only proposal (no new text): relevant skills and bullets first; list missing requirements."""
        cand, ver = self.master(mode_id)
        doc = resume_doc(ver.content or {}, ver.id)
        jdoc = self.app.jd.document(job_id, mode_id)
        weight = {"required": 3, "preferred": 2, "mentioned": 1}
        kw = {k["name"]: weight[k["importance"]] for k in jdoc["jd"]["keywords"]}
        matched = {m["name"]: weight.get(m["importance"], 1) for m in (jdoc.get("match") or {}).get("matched", [])}

        def text_score(text: str) -> int:
            return sum(w for key, *_ in find_vocab(text) for name, w in kw.items() if name == key)

        changes: list[dict[str, Any]] = []
        skill_ids = [s["id"] for s in doc["skills"]]
        ranked = sorted(skill_ids, key=lambda i: (-max(matched.get(i[6:], 0), kw.get(i[6:], 0)), skill_ids.index(i)))
        if ranked != skill_ids:
            top = [i[6:] for i in ranked[:8] if max(matched.get(i[6:], 0), kw.get(i[6:], 0))]
            changes.append({"change_id": "d-skills", "action": "reorder", "target_id": "skills", "order": ranked,
                            "evidence_ids": [], "rationale": "Skills pedidos por la vacante primero: " + ", ".join(top)})
        for p in doc["experience"]:
            ids = [b["id"] for b in p["bullets"]]
            scores = {b["id"]: text_score(b["text"]) for b in p["bullets"]}
            new = sorted(ids, key=lambda i: (-scores[i], ids.index(i)))
            if new != ids:
                changes.append({"change_id": f"d-{p['id']}", "action": "reorder", "target_id": p["id"], "order": new,
                                "evidence_ids": [], "rationale": f"Logros de {p['company'] or p['title']} con más "
                                                                 "palabras clave de la JD primero."})
        missing = [f"{m['name']} ({m['importance']})" for m in (jdoc.get("match") or {}).get("missing", [])
                   if m["importance"] in ("required", "preferred")]
        proposal = {"protocol_version": TAILORING_PROTOCOL, "job_id": job_id, "jd_hash": jdoc["jd"]["hash"],
                    "master_version_id": ver.id, "provider": "jsh", "model": "deterministic-v1", "changes": changes,
                    "missing_requirements": missing,
                    "keywords_used": [k for k in matched][:30],
                    "notes": "Propuesta automática sin IA: solo reordena contenido aprobado; no reescribe textos."}
        return self._store(TailoringProposal.model_validate(proposal), mode_id, "deterministic", actor, warnings=[])

    # ------------------------------------------------------------------ package for external AI
    def build_package(self, job_id: str, mode_id: str, include_personal: bool = False,
                      template_key: str = "tailoring_default", actor: str = "user") -> dict[str, Any]:
        if mode_id not in MODE_DEFS:
            raise ValueError(f"Modo inválido: {mode_id}")
        cand, ver = self.master(mode_id)
        doc = resume_doc(ver.content or {}, ver.id)
        jdoc = self.app.jd.document(job_id, mode_id)
        tpl = self.app.reviews.template(template_key)
        with self.db.session() as s:
            pkg = AIPackage(kind="tailoring", mode_id=mode_id, job_ids=[job_id], jd_hashes={job_id: jdoc["jd"]["hash"]},
                            candidate_id=cand.id, profile_version=cand.version, template_key=tpl.key,
                            template_version=tpl.version, include_personal=include_personal,
                            extra={"master_version_id": ver.id}, actor=actor[:60])
            s.add(pkg)
            s.flush()
            s.add(AuditEvent(entity_type="ai_package", entity_id=pkg.id, action="package_exported", mode_id=mode_id,
                             actor=actor[:60], details={"kind": "tailoring", "job_id": job_id,
                                                        "include_personal": include_personal}))
            s.commit()
            package_id, created = pkg.id, pkg.created_at
        resume = dict(doc)
        if include_personal:
            resume["header"] = dict(cand.contact or {})
        else:
            terms = personal_terms(cand.contact)
            resume = {**mask_data({k: v for k, v in resume.items() if k != "skills"}, terms),
                      "master_version_id": ver.id,
                      "skills": [{**s, "evidence": scrub_list(s.get("evidence", []), terms)} for s in doc["skills"]]}
        return {
            "protocol_version": TAILORING_PROTOCOL, "package_id": package_id, "kind": "tailoring", "mode": mode_id,
            "created_at": created.isoformat(timespec="seconds"), "instructions": tpl.body,
            "template": {"key": tpl.key, "version": tpl.version}, "job_id": job_id, "jd_hash": jdoc["jd"]["hash"],
            "master_version_id": ver.id,
            "approved_skills": [s["name"] for s in doc["skills"]],
            "master_resume": resume,
            "job": {k: jdoc["job"][k] for k in ("title", "company_name", "location_text", "job_country", "work_mode",
                                                 "seniority", "employment_type")},
            "jd": {k: jdoc["jd"][k] for k in ("flat_text", "keywords", "required_keywords", "preferred_keywords",
                                               "years_experience", "education")},
            "skills_match": jdoc.get("match"),
            "response_schema": response_schema(),
            "response_example": {"protocol_version": TAILORING_PROTOCOL, "package_id": package_id, "job_id": job_id,
                                 "jd_hash": jdoc["jd"]["hash"], "master_version_id": ver.id, "provider": "…",
                                 "model": "…", "changes": [{"change_id": "c1", "action": "rewrite",
                                                            "target_id": "P1.H1", "original": "…", "proposed": "…",
                                                            "evidence_ids": ["P1.H1"], "rationale": "…",
                                                            "keywords": ["SQL"]}],
                                 "skills_order": [], "missing_requirements": [], "keywords_used": [],
                                 "questions_for_user": [], "notes": ""},
        }

    # ------------------------------------------------------------------ import + validation
    def import_proposal(self, payload: Any, source: str = "file", actor: str = "user") -> dict[str, Any]:
        data = parse_json_payload(payload)
        if data.get("protocol_version") != TAILORING_PROTOCOL:
            raise ValueError(f"protocol_version debe ser '{TAILORING_PROTOCOL}' (recibido: "
                             f"{str(data.get('protocol_version'))[:40]!r}).")
        try:
            prop = TailoringProposal.model_validate(data)
        except ValidationError as exc:
            raise ValueError(_validation_text(exc)) from exc
        warnings = [f"Campos no reconocidos ignorados: {', '.join(_unknown_keys(prop))[:300]}"] \
            if _unknown_keys(prop) else []
        mode_id = ""
        with self.db.session() as s:
            if s.get(Job, prop.job_id) is None:
                raise ValueError("job_id no existe.")
            ver = s.get(ResumeVersion, prop.master_version_id)
            if ver is None or ver.kind != "master":
                raise ValueError("master_version_id no corresponde a una versión maestra del CV.")
            if prop.package_id:
                pkg = s.get(AIPackage, prop.package_id)
                if pkg is None or pkg.kind != "tailoring":
                    raise ValueError("package_id desconocido (no es un paquete de adaptación exportado por esta app).")
                if prop.job_id not in (pkg.job_ids or []):
                    raise ValueError("job_id no pertenece al paquete.")
                if prop.jd_hash != (pkg.jd_hashes or {}).get(prop.job_id):
                    raise ValueError("jd_hash no coincide con el paquete (el modelo no copió el hash exacto).")
                if (pkg.extra or {}).get("master_version_id") != prop.master_version_id:
                    raise ValueError("master_version_id no coincide con el paquete.")
                mode_id = pkg.mode_id
        if not mode_id:
            for m in MODE_DEFS:
                cand = self.app.candidate.active_candidate(m)
                if cand is not None and cand.id == ver.candidate_id:
                    mode_id = m
                    break
        current = self.app.jd.document(prop.job_id, None, include_text=False)["jd"]["hash"]
        if prop.jd_hash and prop.jd_hash != current:
            warnings.append("La JD cambió desde que se generó la propuesta (desactualizada).")
        if not prop.jd_hash:
            warnings.append("Sin jd_hash: no se puede comprobar que se usó la JD actual.")
        return self._store(prop, mode_id, source, actor, warnings)

    def _store(self, prop: TailoringProposal, mode_id: str, source: str, actor: str,
               warnings: list[str]) -> dict[str, Any]:
        with self.db.session() as s:
            ver = s.get(ResumeVersion, prop.master_version_id)
            doc = resume_doc(ver.content or {}, ver.id)
            candidate_id = ver.candidate_id
        approved = {fold(s["name"]) for s in doc["skills"]}
        texts = element_texts(doc)
        bullets_of = {p["id"]: [b["id"] for b in p["bullets"]] for p in doc["experience"]}
        changes, rejected = [], []
        seen: set[str] = set()
        proposed_changes = list(prop.changes)
        if prop.skills_order:  # becomes a normal, user-approved reorder change (never applied implicitly)
            if any(c.action == "reorder" and c.target_id == "skills" for c in proposed_changes):
                rejected.append({"change_id": "skills_order", "target_id": "skills",
                                 "reason": "skills_order duplicado: ya hay un cambio 'reorder' de skills"})
            else:
                current = [s["id"] for s in doc["skills"]]
                first = list(dict.fromkeys(f"skill:{n}" for n in prop.skills_order if f"skill:{n}" in current))
                order = first + [i for i in current if i not in first]
                if order != current:
                    proposed_changes.append(Change(change_id="skills_order", action="reorder", target_id="skills",
                                                   order=order, rationale="Orden de skills propuesto por la IA"))
        for ch in proposed_changes:
            reason, item = self._check(ch, doc, texts, bullets_of, approved)
            if ch.change_id in seen:
                reason = "change_id repetido"
            if reason:
                rejected.append({"change_id": ch.change_id, "target_id": ch.target_id, "reason": reason})
                continue
            seen.add(ch.change_id)
            changes.append(item)
        with self.db.session() as s:
            run = TailoringRun(job_id=prop.job_id, mode_id=mode_id, candidate_id=candidate_id,
                               master_version_id=prop.master_version_id, package_id=prop.package_id,
                               source=source[:20], provider=prop.provider, model=prop.model,
                               proposal={"changes": changes, "rejected_changes": rejected,
                                         "missing_requirements": prop.missing_requirements,
                                         "keywords_used": prop.keywords_used,
                                         "questions_for_user": prop.questions_for_user, "notes": prop.notes,
                                         "jd_hash": prop.jd_hash},
                               warnings=warnings, actor=actor[:60])
            s.add(run)
            s.flush()
            s.add(AuditEvent(entity_type="job", entity_id=prop.job_id, action="tailoring_proposed", mode_id=mode_id,
                             actor=actor[:60], details={"run_id": run.id, "changes": len(changes),
                                                        "rejected": len(rejected), "source": source}))
            s.commit()
            run_id = run.id
        return {"run_id": run_id, "changes": len(changes), "flagged": sum(1 for c in changes if c["requires_confirmation"]),
                "rejected": rejected, "warnings": warnings}

    @staticmethod
    def _check(ch: Change, doc, texts: dict[str, str], bullets_of: dict[str, list[str]],
               approved: set[str]) -> tuple[str, dict[str, Any]]:
        flags: list[dict[str, str]] = []
        item = {"change_id": ch.change_id, "action": ch.action, "target_id": ch.target_id, "original": "",
                "proposed": ch.proposed, "order": [], "evidence_ids": list(ch.evidence_ids), "rationale": ch.rationale,
                "keywords": list(ch.keywords)}
        target = ch.target_id
        if ch.action == "reorder":
            if target == "skills":
                current = [s["id"] for s in doc["skills"]]
            elif target in bullets_of:
                current = bullets_of[target]
            else:
                return "reorder solo aplica a 'skills' o a un puesto (P1, P2…)", item
            order = list(ch.order)
            if target == "skills":
                order = [i if i.startswith("skill:") else f"skill:{i}" for i in order]
            if sorted(order) != sorted(current):
                return "el nuevo orden debe contener exactamente los mismos elementos", item
            item["order"] = order
            return "", {**item, "flags": [], "requires_confirmation": False}
        if ch.action == "add":
            if target not in bullets_of:
                return "add solo puede agregar un logro a un puesto existente (P1, P2…)", item
            if not ch.proposed:
                return "add sin texto propuesto", item
            flags.append({"code": "new_text", "severity": "high",
                          "message": "Texto NUEVO: confirme que es verdadero y comprobable."})
        else:
            if target not in texts or target.startswith("skill:") or re.fullmatch(r"P\d+", target) \
                    or re.fullmatch(r"[ECL]\d+", target):
                return "solo se pueden reescribir/quitar el resumen (S1…) o logros (P1.H2…); nunca puestos, " \
                       "empresas, fechas o estudios", item
            item["original"] = texts[target]
            if ch.original and _norm(ch.original) != _norm(texts[target]):
                flags.append({"code": "original_mismatch", "severity": "low",
                              "message": "El 'original' enviado no coincide; se usa el texto real del CV maestro."})
            if ch.action == "rewrite" and not ch.proposed:
                return "rewrite sin texto propuesto", item
        if ch.action in ("rewrite", "add"):
            evidence_ids = list(dict.fromkeys(ch.evidence_ids or ([target] if ch.action == "rewrite" else [])))
            unknown = [e for e in evidence_ids if e not in texts]
            if unknown:
                flags.append({"code": "unknown_evidence", "severity": "high",
                              "message": f"Evidencia inexistente: {', '.join(unknown)[:200]}"})
            evidence_ids = [e for e in evidence_ids if e in texts]
            if not evidence_ids:
                flags.append({"code": "no_evidence", "severity": "high", "message": "Sin evidencia del CV maestro."})
            support = " ".join(texts[e] for e in evidence_ids) + " " + item["original"]
            new_numbers = sorted(_numbers(ch.proposed) - _numbers(support))
            if new_numbers:
                flags.append({"code": "new_numbers", "severity": "high",
                              "message": f"Cifras que no están en la evidencia: {', '.join(new_numbers)[:200]}"})
            support_keys = {k for k, *_ in find_vocab(support)}
            new_skills = sorted({k for k, *_ in find_vocab(ch.proposed)
                                 if k not in support_keys and fold(k) not in approved
                                 and fold(BY_KEY[k].key) not in approved})
            if new_skills:
                flags.append({"code": "unapproved_skill", "severity": "high",
                              "message": f"Skills no aprobados ni en la evidencia: {', '.join(new_skills)[:200]}"})
            allowed = {w for name in approved for w in name.split()} | {fold(k) for k in support_keys}
            terms = [t for t in new_fact_terms(ch.proposed, support, allowed)
                     if fold(t) not in {fold(s) for s in new_skills}]
            if terms:
                flags.append({"code": "new_terms", "severity": "high",
                              "message": "Términos nuevos (nombres, cargos, fechas o cantidades) que no están en la "
                                         f"evidencia: {', '.join(terms)[:200]}"})
            if len(ch.proposed) > MAX_TEXT:
                flags.append({"code": "too_long", "severity": "low",
                              "message": f"Texto largo ({len(ch.proposed)} caracteres)."})
            item["evidence_ids"] = evidence_ids
        return "", {**item, "flags": flags, "requires_confirmation": any(f["severity"] == "high" for f in flags)}

    # ------------------------------------------------------------------ review / approve
    def runs(self, job_id: str | None = None, mode_id: str | None = None, status: str | None = None
             ) -> list[dict[str, Any]]:
        with self.db.session() as s:
            stmt = select(TailoringRun, Job.title, Job.company_name).join(Job, Job.id == TailoringRun.job_id)
            if job_id:
                stmt = stmt.where(TailoringRun.job_id == job_id)
            if mode_id:
                stmt = stmt.where(TailoringRun.mode_id == mode_id)
            if status:
                stmt = stmt.where(TailoringRun.status == status)
            rows = s.execute(stmt.order_by(TailoringRun.created_at.desc())).all()
        return [self._run_dict(r, t, c) for r, t, c in rows]

    @staticmethod
    def _run_dict(r: TailoringRun, title: str = "", company: str = "") -> dict[str, Any]:
        p = r.proposal or {}
        return {"run_id": r.id, "job_id": r.job_id, "title": title, "company_name": company, "mode_id": r.mode_id,
                "status": r.status, "source": r.source, "provider": r.provider, "model": r.model,
                "master_version_id": r.master_version_id, "package_id": r.package_id,
                "changes": p.get("changes", []), "rejected_changes": p.get("rejected_changes", []),
                "missing_requirements": p.get("missing_requirements", []),
                "questions_for_user": p.get("questions_for_user", []), "notes": p.get("notes", ""),
                "warnings": list(r.warnings or []), "resume_version_id": r.resume_version_id,
                "approved_change_ids": list(r.approved_change_ids or []),
                "created_at": r.created_at.isoformat(timespec="minutes")}

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            r = s.get(TailoringRun, run_id)
            if r is None:
                raise KeyError(run_id)
            job = s.get(Job, r.job_id)
            return self._run_dict(r, job.title if job else "", job.company_name if job else "")

    @staticmethod
    def apply(doc: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
        out = json.loads(json.dumps(doc))
        by_bullet = {b["id"]: (p, b) for p in out["experience"] for b in p["bullets"]}
        positions = {p["id"]: p for p in out["experience"]}
        added = 0
        for ch in changes:
            t = ch["target_id"]
            if ch["action"] == "rewrite":
                if t in by_bullet:
                    by_bullet[t][1]["text"] = ch["proposed"]
                else:
                    for sm in out["summary"]:
                        if sm["id"] == t:
                            sm["text"] = ch["proposed"]
            elif ch["action"] == "remove":
                if t in by_bullet:
                    p, b = by_bullet[t]
                    p["bullets"] = [x for x in p["bullets"] if x["id"] != t]
                else:
                    out["summary"] = [x for x in out["summary"] if x["id"] != t]
            elif ch["action"] == "add" and t in positions:
                added += 1
                positions[t]["bullets"].append({"id": f"{t}.N{added}", "text": ch["proposed"]})
            elif ch["action"] == "reorder":
                if t == "skills":
                    idx = {s["id"]: s for s in out["skills"]}
                    out["skills"] = [idx[i] for i in ch["order"] if i in idx]
                elif t in positions:
                    idx = {b["id"]: b for b in positions[t]["bullets"]}
                    rest = [b for b in positions[t]["bullets"] if b["id"] not in ch["order"]]
                    positions[t]["bullets"] = [idx[i] for i in ch["order"] if i in idx] + rest
        return out

    def approve(self, run_id: str, change_ids: list[str], confirmed_ids: list[str] | None = None,
                name: str = "", link_application: bool = True, actor: str = "user") -> str:
        """Apply the selected changes to the master → new approved tailored ResumeVersion. Returns its id."""
        confirmed = set(confirmed_ids or [])
        run = self.get_run(run_id)
        if run["status"] != "proposed":
            raise ValueError("Esta propuesta ya fue decidida.")
        chosen = [c for c in run["changes"] if c["change_id"] in set(change_ids)]
        unconfirmed = [c["change_id"] for c in chosen if c["requires_confirmation"] and c["change_id"] not in confirmed]
        if unconfirmed:
            raise ValueError("Confirme que verificó los cambios marcados antes de aprobar: " + ", ".join(unconfirmed))
        with self.db.session() as s:
            master = s.get(ResumeVersion, run["master_version_id"])
            job = s.get(Job, run["job_id"])
            doc = self.apply(resume_doc(master.content or {}, master.id), chosen)
            n = (s.scalar(select(func.count()).select_from(ResumeVersion).where(
                ResumeVersion.kind == "tailored", ResumeVersion.job_id == job.id)) or 0) + 1
            tr = s.get(TailoringRun, run_id)
            pkg = s.get(AIPackage, tr.package_id) if tr.package_id else None
            ver = ResumeVersion(
                candidate_id=master.candidate_id, kind="tailored", version=n, job_id=job.id,
                name=(name.strip() or f"{job.company_name or 'Empresa'} — {job.title} v{n}")[:200], content=doc,
                provenance={"master_version_id": master.id, "tailoring_run_id": run_id, "mode_id": run["mode_id"],
                            "source": run["source"], "provider": run["provider"], "model": run["model"],
                            "package_id": run["package_id"],
                            "prompt_template": {"key": pkg.template_key, "version": pkg.template_version} if pkg else None,
                            "jd_hash": (tr.proposal or {}).get("jd_hash", ""),
                            "approved_change_ids": [c["change_id"] for c in chosen],
                            "rejected_change_ids": [c["change_id"] for c in run["changes"] if c not in chosen],
                            "confirmed_flagged_ids": sorted(confirmed & {c["change_id"] for c in chosen}),
                            "approved_by": actor},
                approved=True, approved_at=utcnow())
            s.add(ver)
            s.flush()
            tr.status, tr.approved_change_ids, tr.resume_version_id, tr.decided_at = \
                "approved", [c["change_id"] for c in chosen], ver.id, utcnow()
            if link_application and run["mode_id"]:
                app = s.scalar(select(Application).where(Application.job_id == job.id,
                                                         Application.mode_id == run["mode_id"]))
                if app is None:
                    app = Application(job_id=job.id, mode_id=run["mode_id"], status="discovered")
                    s.add(app)
                app.resume_version_id = ver.id
            s.add(AuditEvent(entity_type="job", entity_id=job.id, action="tailored_resume_approved",
                             mode_id=run["mode_id"], actor=actor[:60],
                             details={"resume_version_id": ver.id, "run_id": run_id, "changes": len(chosen)}))
            s.commit()
            return ver.id

    def discard(self, run_id: str, actor: str = "user") -> None:
        with self.db.session() as s:
            tr = s.get(TailoringRun, run_id)
            if tr is None:
                raise KeyError(run_id)
            if tr.status == "proposed":
                tr.status, tr.decided_at = "discarded", utcnow()
                s.add(AuditEvent(entity_type="job", entity_id=tr.job_id, action="tailoring_discarded",
                                 mode_id=tr.mode_id, actor=actor[:60], details={"run_id": run_id}))
                s.commit()

    # ------------------------------------------------------------------ documents
    def tailored_versions(self, job_id: str | None = None) -> list[ResumeVersion]:
        with self.db.session() as s:
            stmt = select(ResumeVersion).where(ResumeVersion.kind == "tailored")
            if job_id:
                stmt = stmt.where(ResumeVersion.job_id == job_id)
            return list(s.scalars(stmt.order_by(ResumeVersion.created_at.desc())))

    def render(self, version_id: str, lang: str | None = None) -> str:
        with self.db.session() as s:
            ver = s.get(ResumeVersion, version_id)
            cand = s.get(CandidateProfile, ver.candidate_id)
            header = dict(cand.contact or {}) if cand and isinstance(cand.contact, dict) else {}
        return render_text(ver.content, header, lang or self.app.db.get_setting("resume_language", "en"))

    def generate_docx(self, version_id: str, lang: str | None = None) -> str:
        """Render an approved tailored version to resumes/tailored/*.docx. Returns the path relative to home."""
        from docx import Document
        from docx.shared import Pt

        lang = lang or self.app.db.get_setting("resume_language", "en")
        t = SECTION_TITLES.get(lang, SECTION_TITLES["en"])
        with self.db.session() as s:
            ver = s.get(ResumeVersion, version_id)
            if ver is None or not ver.approved or ver.kind != "tailored":
                raise ValueError("Solo se generan documentos de versiones adaptadas aprobadas.")
            cand = s.get(CandidateProfile, ver.candidate_id)
            header = dict(cand.contact or {}) if cand and isinstance(cand.contact, dict) else {}
            job = s.get(Job, ver.job_id) if ver.job_id else None
            doc_data, n = ver.content, ver.version
        d = Document()
        d.styles["Normal"].font.name = "Calibri"
        d.styles["Normal"].font.size = Pt(10.5)
        d.add_heading(header.get("name") or doc_data.get("candidate") or "Curriculum Vitae", level=0)
        if header.get("headline"):
            d.add_paragraph(header["headline"])
        contact = " | ".join(x for x in (header.get("email"), header.get("phone"), header.get("location"),
                                         *(header.get("links") or [])) if x)
        if contact:
            d.add_paragraph(contact)
        if doc_data.get("summary"):
            d.add_heading(t["summary"], level=1)
            for sm in doc_data["summary"]:
                d.add_paragraph(sm["text"])
        if doc_data.get("experience"):
            d.add_heading(t["experience"], level=1)
            for p in doc_data["experience"]:
                para = d.add_paragraph()
                para.add_run(" — ".join(x for x in (p["title"], p["company"]) if x)).bold = True
                meta = " · ".join(x for x in (p["location"], p["dates"]) if x)
                if meta:
                    para.add_run(f"\n{meta}").italic = True
                for b in p["bullets"]:
                    d.add_paragraph(b["text"], style="List Bullet")
        if doc_data.get("skills"):
            d.add_heading(t["skills"], level=1)
            d.add_paragraph(", ".join(s["name"] for s in doc_data["skills"]))
        for key in ("education", "certifications", "languages"):
            if doc_data.get(key):
                d.add_heading(t[key], level=1)
                for e in doc_data[key]:
                    d.add_paragraph(", ".join(str(v) for k, v in e.items() if k != "id" and v))
        folder = self.app.paths.resumes / "tailored"
        folder.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{job.company_name}_{job.title}" if job else "resume")[:80].strip("_")
        path = folder / f"{stem or 'resume'}_v{n}_{version_id[:6]}.docx"
        d.save(path)
        rel = path.relative_to(self.app.paths.home).as_posix()
        with self.db.session() as s:
            ver = s.get(ResumeVersion, version_id)
            ver.provenance = {**(ver.provenance or {}), "docx": rel, "docx_lang": lang}
            s.add(AuditEvent(entity_type="resume_version", entity_id=version_id, action="docx_generated",
                             actor="user", details={"path": rel}))
            s.commit()
        return rel
