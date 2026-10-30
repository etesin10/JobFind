"""CandidateProfileService / SkillsRepository (Phase 2).

Rules enforced here (spec §3):
- The original résumé file is copied unchanged to ``resumes/originals/`` and identified by SHA-256.
- Everything extracted is a PROPOSAL; only user-approved skills/items enter the approved profile.
- Proficiency and years are only stored when the user enters them (never inferred).
- Approved-profile changes bump ``CandidateProfile.version`` and re-evaluate jobs in the modes that use it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..candidate.extraction import extract
from ..candidate.matching import ApprovedSkill, SkillMatcher
from ..candidate.parsing import ParsedResume, ResumeParseError, parse_resume, parse_text
from ..candidate.vocabulary import BY_KEY, CATEGORY_LABELS, find_vocab, norm_phrase
from ..models import (
    AuditEvent, CandidateProfile, ProfileItem, ResumeSource, ResumeVersion, Setting, Skill, SkillEvidence, utcnow,
)
from ..modes import MODE_DEFS

SKILL_STATUSES = ("proposed", "approved", "rejected")
ITEM_KINDS = ("summary", "position", "education", "certification", "language")
ITEM_FIELDS = {
    "summary": ("text",),
    "position": ("title", "company", "location", "start", "end", "end_note", "dates_text", "highlights"),
    "education": ("degree", "institution", "year"),
    "certification": ("name", "issuer", "year"),
    "language": ("language", "level", "as_written"),
}
TAGS = ("", "core", "preferred")
MAX_EVIDENCE_PER_SOURCE = 6
AUTO_APPROVE_KEY = "auto_approve_high_confidence"


@dataclass
class ImportResult:
    source_id: str
    duplicate: bool = False
    new_skills: int = 0
    evidence_added: int = 0
    new_items: int = 0
    skipped_items: int = 0
    auto_approved: int = 0
    warnings: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)


def _safe_name(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:80].strip("._")
    return stem or "resume"


class CandidateService:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    # ------------------------------------------------------------------ profiles
    def list_candidates(self) -> list[CandidateProfile]:
        with self.db.session() as s:
            return list(s.scalars(select(CandidateProfile).order_by(CandidateProfile.created_at)))

    def get(self, candidate_id: str) -> CandidateProfile | None:
        with self.db.session() as s:
            return s.get(CandidateProfile, candidate_id)

    def create_candidate(self, name: str, actor: str = "user") -> CandidateProfile:
        name = (name or "").strip()
        if not name:
            raise ValueError("El perfil de candidato necesita un nombre.")
        with self.db.session() as s:
            if s.scalar(select(CandidateProfile).where(func.lower(CandidateProfile.name) == name.lower())):
                raise ValueError(f"Ya existe un perfil de candidato llamado '{name}'.")
            cand = CandidateProfile(name=name)
            s.add(cand)
            s.flush()
            self._audit(s, cand.id, "candidate_created", actor, name=name)
            s.commit()
            return cand

    def rename_candidate(self, candidate_id: str, name: str, actor: str = "user") -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("El perfil de candidato necesita un nombre.")
        with self.db.session() as s:
            cand = s.get(CandidateProfile, candidate_id)
            if cand is None:
                raise KeyError(candidate_id)
            clash = s.scalar(select(CandidateProfile).where(func.lower(CandidateProfile.name) == name.lower(),
                                                            CandidateProfile.id != candidate_id))
            if clash:
                raise ValueError(f"Ya existe un perfil de candidato llamado '{name}'.")
            old, cand.name = cand.name, name
            self._audit(s, candidate_id, "candidate_renamed", actor, old=old, new=name)
            s.commit()

    def delete_candidate(self, candidate_id: str, actor: str = "user") -> None:
        """Delete a profile that no mode uses. Original résumé files stay on disk in resumes/originals."""
        if self.modes_using(candidate_id):
            raise ValueError("Este perfil está activo en un modo. Active otro perfil antes de eliminarlo.")
        with self.db.session() as s:
            cand = s.get(CandidateProfile, candidate_id)
            if cand is None:
                return
            skill_ids = list(s.scalars(select(Skill.id).where(Skill.candidate_id == candidate_id)))
            if skill_ids:
                s.execute(delete(SkillEvidence).where(SkillEvidence.skill_id.in_(skill_ids)))
            for model in (Skill, ProfileItem, ResumeVersion, ResumeSource):
                s.execute(delete(model).where(model.candidate_id == candidate_id))
            self._audit(s, candidate_id, "candidate_deleted", actor, name=cand.name)
            s.delete(cand)
            s.commit()

    def active_candidate_id(self, s: Session, mode_id: str) -> str | None:
        row = s.get(Setting, f"active_candidate:{mode_id}")
        if row and row.value and s.get(CandidateProfile, row.value) is not None:
            return row.value
        first = s.scalar(select(CandidateProfile.id).order_by(CandidateProfile.created_at).limit(1))
        return first

    def active_candidate(self, mode_id: str) -> CandidateProfile | None:
        with self.db.session() as s:
            cid = self.active_candidate_id(s, mode_id)
            return s.get(CandidateProfile, cid) if cid else None

    def set_active_candidate(self, mode_id: str, candidate_id: str, actor: str = "user") -> None:
        if mode_id not in MODE_DEFS:
            raise ValueError(mode_id)
        with self.db.session() as s:
            if s.get(CandidateProfile, candidate_id) is None:
                raise KeyError(candidate_id)
            key = f"active_candidate:{mode_id}"
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=candidate_id))
            else:
                row.value = candidate_id
            self._audit(s, candidate_id, "candidate_activated", actor, mode=mode_id)
            s.commit()
        self.app.jobs.reevaluate_mode(mode_id)

    def modes_using(self, candidate_id: str) -> list[str]:
        with self.db.session() as s:
            return [m for m in MODE_DEFS if self.active_candidate_id(s, m) == candidate_id]

    # ------------------------------------------------------------------ import
    def import_file(self, candidate_id: str, file_name: str, data: bytes, actor: str = "user") -> ImportResult:
        parsed = parse_resume(data, file_name)  # raises ResumeParseError (user-facing message)
        return self._import(candidate_id, file_name, data, parsed, actor)

    def import_text(self, candidate_id: str, text: str, label: str = "", actor: str = "user") -> ImportResult:
        if len((text or "").strip()) < 20:
            raise ResumeParseError("Pegue el texto completo del currículum (muy corto).")
        parsed = parse_text(text, kind="paste")
        name = (label.strip() or "texto_pegado") + ".txt"
        return self._import(candidate_id, name, text.encode("utf-8"), parsed, actor)

    def _import(self, candidate_id: str, file_name: str, data: bytes, parsed: ParsedResume,
                actor: str) -> ImportResult:
        sha = hashlib.sha256(data).hexdigest()
        with self.db.session() as s:
            if s.get(CandidateProfile, candidate_id) is None:
                raise KeyError(candidate_id)
            existing = s.scalar(select(ResumeSource).where(ResumeSource.candidate_id == candidate_id,
                                                           ResumeSource.sha256 == sha))
            if existing is not None:
                return ImportResult(existing.id, duplicate=True)

            originals = self.app.paths.resumes / "originals"
            originals.mkdir(parents=True, exist_ok=True)
            stored_name = f"{sha[:12]}_{_safe_name(file_name)}"
            target = originals / stored_name
            if not target.exists():
                target.write_bytes(data)  # written once, never modified afterwards
            source = ResumeSource(candidate_id=candidate_id, file_name=file_name[:300], kind=parsed.kind,
                                  stored_path=f"resumes/originals/{stored_name}", sha256=sha, text=parsed.text,
                                  warnings=list(parsed.warnings))
            s.add(source)
            s.flush()

            ex = extract(parsed.lines)
            result = ImportResult(source.id, warnings=list(parsed.warnings), sections=sorted(set(ex.sections_found)))
            auto = bool(self.app.db.get_setting(AUTO_APPROVE_KEY, False))
            skills = {sk.normalized: sk for sk in s.scalars(select(Skill).where(Skill.candidate_id == candidate_id))}
            for prop in ex.skills:
                key = norm_phrase(prop.name)
                sk = skills.get(key)
                if sk is None:
                    # Optional user setting: HIGH-confidence skills (found in the experience section) are approved
                    # right away; everything else stays a proposal for review.
                    status = "approved" if auto and prop.confidence == "high" else "proposed"
                    sk = Skill(candidate_id=candidate_id, name=prop.name, normalized=key, vocab_key=prop.vocab_key,
                               category=prop.category, status=status, confidence=prop.confidence,
                               origin="resume", notes=prop.notes, aliases=[])
                    s.add(sk)
                    s.flush()
                    skills[key] = sk
                    result.new_skills += 1
                    if status == "approved":
                        result.auto_approved += 1
                elif auto and prop.confidence == "high" and sk.status == "proposed" and sk.origin == "resume":
                    sk.status = "approved"  # never overrides a skill the user REJECTED
                    result.auto_approved += 1
                for ev in prop.evidence[:MAX_EVIDENCE_PER_SOURCE]:
                    s.add(SkillEvidence(skill_id=sk.id, source_id=source.id, section=ev.section,
                                        excerpt=ev.excerpt[:600]))
                    result.evidence_added += 1

            known = {(it.kind, it.dedupe_key) for it in s.scalars(select(ProfileItem).where(
                ProfileItem.candidate_id == candidate_id))}
            for item in ex.items:
                if (item.kind, item.dedupe_key) in known:
                    result.skipped_items += 1
                    continue
                known.add((item.kind, item.dedupe_key))
                s.add(ProfileItem(candidate_id=candidate_id, source_id=source.id, kind=item.kind,
                                  dedupe_key=item.dedupe_key[:300], data=item.data, confidence=item.confidence,
                                  excerpt=item.excerpt[:600], position=item.position))
                result.new_items += 1
            source.summary = {"new_skills": result.new_skills, "evidence": result.evidence_added,
                              "new_items": result.new_items, "skipped_items": result.skipped_items,
                              "sections": result.sections, "auto_approved": result.auto_approved}
            # Audit records counts only: no résumé text or personal data in the log.
            self._audit(s, candidate_id, "resume_imported", actor, file=file_name[:120], kind=parsed.kind,
                        sha256=sha[:12], **source.summary)
            if result.auto_approved:
                s.get(CandidateProfile, candidate_id).version += 1
            s.commit()
        self._reevaluate(candidate_id)
        return result

    def propose_skills(self, candidate_id: str, items: list[dict[str, Any]], actor: str) -> dict[str, Any]:
        """Skills suggested by an external AI agent: always PROPOSED (origin 'agent'), never approved, never touching
        existing skills. Each item: {name, category?, aliases?, evidence?}. Returns counts and per-item notes."""
        if not isinstance(items, list) or not items:
            raise ValueError("Envíe una lista de skills: [{\"name\": ..., \"evidence\": ...}]")
        if len(items) > 100:
            raise ValueError("Máximo 100 skills por envío.")
        out: dict[str, Any] = {"proposed": 0, "existing": [], "ignored": []}
        with self.db.session() as s:
            if s.get(CandidateProfile, candidate_id) is None:
                raise KeyError(candidate_id)
            have = {sk.normalized: sk for sk in s.scalars(select(Skill).where(Skill.candidate_id == candidate_id))}
            for it in items:
                name = str((it or {}).get("name") if isinstance(it, dict) else it or "").strip()[:120]
                if len(name) < 1:
                    out["ignored"].append("(sin nombre)")
                    continue
                key = norm_phrase(name)
                if key in have:
                    out["existing"].append(f"{have[key].name} ({have[key].status})")
                    continue
                vkey = self._vocab_key_for(name)
                cat = str(it.get("category") or "") if isinstance(it, dict) else ""
                cat = cat if cat in CATEGORY_LABELS else (BY_KEY[vkey].category if vkey else "other")
                aliases = it.get("aliases") if isinstance(it, dict) else None
                aliases = [str(a).strip()[:80] for a in (aliases if isinstance(aliases, list) else []) if str(a).strip()][:10]
                sk = Skill(candidate_id=candidate_id, name=BY_KEY[vkey].key if vkey else name, normalized=key,
                           vocab_key=vkey or "", category=cat, status="proposed", confidence="",
                           origin="agent", notes=f"Sugerido por {actor}"[:200], aliases=aliases)
                s.add(sk)
                s.flush()
                have[key] = sk
                evidence = str(it.get("evidence") or "").strip() if isinstance(it, dict) else ""
                if evidence:
                    s.add(SkillEvidence(skill_id=sk.id, source_id=None, section="agent", excerpt=evidence[:600]))
                out["proposed"] += 1
            if out["proposed"]:
                self._audit(s, candidate_id, "skills_proposed", actor, count=out["proposed"])
            s.commit()
        return out

    def sources(self, candidate_id: str) -> list[ResumeSource]:
        with self.db.session() as s:
            return list(s.scalars(select(ResumeSource).where(ResumeSource.candidate_id == candidate_id)
                                  .order_by(ResumeSource.imported_at.desc())))

    def verify_original(self, source_id: str) -> bool:
        """True if the stored original file is present and unchanged (SHA-256 matches)."""
        with self.db.session() as s:
            src = s.get(ResumeSource, source_id)
        if src is None or not src.stored_path:
            return False
        path = self.app.paths.home / src.stored_path
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == src.sha256

    # ------------------------------------------------------------------ skills
    def skills(self, candidate_id: str, status: str | None = None) -> list[Skill]:
        with self.db.session() as s:
            stmt = select(Skill).where(Skill.candidate_id == candidate_id)
            if status:
                stmt = stmt.where(Skill.status == status)
            return list(s.scalars(stmt.order_by(Skill.category, func.lower(Skill.name))))

    def get_skill(self, skill_id: str) -> Skill | None:
        with self.db.session() as s:
            return s.get(Skill, skill_id)

    def evidence(self, skill_id: str) -> list[SkillEvidence]:
        with self.db.session() as s:
            return list(s.scalars(select(SkillEvidence).where(SkillEvidence.skill_id == skill_id)
                                  .order_by(SkillEvidence.created_at)))

    def evidence_map(self, candidate_id: str) -> dict[str, list[SkillEvidence]]:
        with self.db.session() as s:
            rows = s.execute(select(SkillEvidence).join(Skill, Skill.id == SkillEvidence.skill_id)
                             .where(Skill.candidate_id == candidate_id).order_by(SkillEvidence.created_at)).scalars()
            out: dict[str, list[SkillEvidence]] = {}
            for ev in rows:
                out.setdefault(ev.skill_id, []).append(ev)
            return out

    def set_skill_status(self, skill_ids: list[str], status: str, actor: str = "user") -> int:
        if status not in SKILL_STATUSES:
            raise ValueError(f"Estado inválido: {status}")
        changed = 0
        candidates: set[str] = set()
        with self.db.session() as s:
            for sk in s.scalars(select(Skill).where(Skill.id.in_(list(skill_ids)))):
                if sk.status == status:
                    continue
                old, sk.status = sk.status, status
                sk.version += 1
                changed += 1
                candidates.add(sk.candidate_id)
                self._audit(s, sk.id, f"skill_{status}", actor, entity_type="skill", name=sk.name, previous=old)
            for cid in candidates:
                s.get(CandidateProfile, cid).version += 1
            s.commit()
        for cid in candidates:
            self._reevaluate(cid)
        return changed

    def update_skill(self, skill_id: str, data: dict[str, Any], actor: str = "user") -> Skill:
        with self.db.session() as s:
            sk = s.get(Skill, skill_id)
            if sk is None:
                raise KeyError(skill_id)
            clean = self._clean_skill_fields(data)
            if "name" in clean:
                key = norm_phrase(clean["name"])
                clash = s.scalar(select(Skill).where(Skill.candidate_id == sk.candidate_id,
                                                     Skill.normalized == key, Skill.id != skill_id))
                if clash:
                    raise ValueError(f"Ya existe el skill '{clash.name}' en este perfil.")
                clean["normalized"] = key
                if not sk.vocab_key or norm_phrase(sk.vocab_key) != key:
                    clean["vocab_key"] = self._vocab_key_for(clean["name"])
            if "status" in clean and clean["status"] not in SKILL_STATUSES:
                raise ValueError(f"Estado inválido: {clean['status']}")
            changed = {k: v for k, v in clean.items() if getattr(sk, k) != v}
            if not changed:
                return sk
            for k, v in changed.items():
                setattr(sk, k, v)
            sk.version += 1
            if sk.status == "approved" or "status" in changed:
                s.get(CandidateProfile, sk.candidate_id).version += 1
            self._audit(s, sk.id, "skill_updated", actor, entity_type="skill", name=sk.name,
                        fields=sorted(changed))
            s.commit()
            cid = sk.candidate_id
        self._reevaluate(cid)
        return sk

    def add_manual_skill(self, candidate_id: str, name: str, category: str = "", aliases=None,
                         actor: str = "user", **extra) -> Skill:
        """A skill the USER asserts (approved immediately, evidence = 'manual')."""
        name = (name or "").strip()
        if not name or len(name) > 120:
            raise ValueError("Escriba el nombre del skill (máx. 120 caracteres).")
        key = norm_phrase(name)
        with self.db.session() as s:
            if s.get(CandidateProfile, candidate_id) is None:
                raise KeyError(candidate_id)
            existing = s.scalar(select(Skill).where(Skill.candidate_id == candidate_id, Skill.normalized == key))
            if existing is not None:
                raise ValueError(f"'{existing.name}' ya existe (estado: {existing.status}). Edítelo en la lista.")
            vocab_key = self._vocab_key_for(name)
            clean = self._clean_skill_fields({"aliases": aliases or [], **extra})
            sk = Skill(candidate_id=candidate_id, name=name, normalized=key, vocab_key=vocab_key,
                       category=category if category in CATEGORY_LABELS else
                       (BY_KEY[vocab_key].category if vocab_key else "other"),
                       status="approved", origin="manual", confidence="", **clean)
            s.add(sk)
            s.flush()
            s.add(SkillEvidence(skill_id=sk.id, source_id=None, section="manual",
                                excerpt="Agregado manualmente por el usuario."))
            s.get(CandidateProfile, candidate_id).version += 1
            self._audit(s, sk.id, "skill_added_manual", actor, entity_type="skill", name=name)
            s.commit()
        self._reevaluate(candidate_id)
        return sk

    @staticmethod
    def _vocab_key_for(name: str) -> str:
        key = norm_phrase(name)
        for entry in BY_KEY.values():
            if norm_phrase(entry.key) == key or any(norm_phrase(a) == key for a in entry.aliases):
                return entry.key
        hits = find_vocab(name)
        if len(hits) == 1 and hits[0][2] - hits[0][1] >= len(name.strip()) - 2:
            return hits[0][0]
        return ""

    @staticmethod
    def _clean_skill_fields(data: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "name" in data:
            name = str(data["name"] or "").strip()
            if not name or len(name) > 120:
                raise ValueError("El nombre del skill es obligatorio (máx. 120 caracteres).")
            out["name"] = name
        if "category" in data:
            cat = str(data["category"] or "other")
            out["category"] = cat if cat in CATEGORY_LABELS else "other"
        if "aliases" in data:
            raw = data["aliases"]
            items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,;\n|]", str(raw or ""))
            aliases = []
            for a in items:
                a = str(a).strip()
                if a and len(a) <= 80 and a.lower() not in (x.lower() for x in aliases):
                    aliases.append(a)
            out["aliases"] = aliases[:20]
        if "tag" in data:
            tag = str(data["tag"] or "")
            if tag not in TAGS:
                raise ValueError("Etiqueta inválida (use core, preferred o vacío).")
            out["tag"] = tag
        if "weight" in data:
            try:
                w = float(data["weight"])
            except (TypeError, ValueError) as exc:
                raise ValueError("El peso debe ser un número entre 0.1 y 5.") from exc
            if not 0.1 <= w <= 5:
                raise ValueError("El peso debe ser un número entre 0.1 y 5.")
            out["weight"] = w
        if "proficiency" in data:
            out["proficiency"] = str(data["proficiency"] or "").strip()[:40]
        if "years" in data:
            y = data["years"]
            if y in ("", None):
                out["years"] = None
            else:
                try:
                    yf = float(y)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Años debe ser un número (0–60) o vacío.") from exc
                if not 0 <= yf <= 60:
                    raise ValueError("Años debe ser un número (0–60) o vacío.")
                out["years"] = yf
        if "notes" in data:
            out["notes"] = str(data["notes"] or "").strip()[:1000]
        if "active" in data:
            v = data["active"]
            out["active"] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "si", "sí")
        if "status" in data:
            out["status"] = str(data["status"] or "")
        return out

    # ------------------------------------------------------------------ items
    def items(self, candidate_id: str, status: str | None = None) -> list[ProfileItem]:
        with self.db.session() as s:
            stmt = select(ProfileItem).where(ProfileItem.candidate_id == candidate_id)
            if status:
                stmt = stmt.where(ProfileItem.status == status)
            return list(s.scalars(stmt.order_by(ProfileItem.kind, ProfileItem.position)))

    def set_item_status(self, item_ids: list[str], status: str, actor: str = "user") -> int:
        if status not in SKILL_STATUSES:
            raise ValueError(f"Estado inválido: {status}")
        changed = 0
        with self.db.session() as s:
            cids = set()
            for it in s.scalars(select(ProfileItem).where(ProfileItem.id.in_(list(item_ids)))):
                if it.status != status:
                    old, it.status = it.status, status
                    changed += 1
                    cids.add(it.candidate_id)
                    self._audit(s, it.id, f"item_{status}", actor, entity_type="profile_item", kind=it.kind,
                                previous=old)
            for cid in cids:
                s.get(CandidateProfile, cid).version += 1
            s.commit()
        return changed

    def update_item(self, item_id: str, data: dict[str, Any], actor: str = "user") -> ProfileItem:
        with self.db.session() as s:
            it = s.get(ProfileItem, item_id)
            if it is None:
                raise KeyError(item_id)
            allowed = ITEM_FIELDS.get(it.kind, ())
            new = dict(it.data or {})
            for k, v in data.items():
                if k not in allowed:
                    continue
                if k == "highlights":
                    lines = v if isinstance(v, list) else str(v or "").split("\n")
                    new[k] = [h if isinstance(h, dict) else {"text": str(h).strip(), "quantified": False}
                              for h in lines if (h.get("text") if isinstance(h, dict) else str(h).strip())][:30]
                else:
                    new[k] = str(v or "").strip()[:2000]
            if new != it.data:
                it.data = new
                if it.status == "approved":
                    s.get(CandidateProfile, it.candidate_id).version += 1
                self._audit(s, it.id, "item_updated", actor, entity_type="profile_item", kind=it.kind)
                s.commit()
            return it

    # ------------------------------------------------------------------ master résumé versions
    CONTACT_FIELDS = ("name", "headline", "email", "phone", "location", "links")

    def update_contact(self, candidate_id: str, data: dict[str, Any], actor: str = "user") -> dict[str, Any]:
        """Personal header used ONLY to render résumé documents locally (never sent in AI packages by default)."""
        with self.db.session() as s:
            cand = s.get(CandidateProfile, candidate_id)
            if cand is None:
                raise KeyError(candidate_id)
            new = dict(cand.contact or {}) if isinstance(cand.contact, dict) else {}
            for k in self.CONTACT_FIELDS:
                if k in data:
                    v = data[k]
                    if k == "links":
                        items = v if isinstance(v, list) else re.split(r"[\n,;]+", str(v or ""))
                        new[k] = [str(x).strip()[:300] for x in items if str(x).strip()][:6]
                    else:
                        new[k] = str(v or "").strip()[:300]
            if new != cand.contact:
                cand.contact = new
                self._audit(s, candidate_id, "contact_updated", actor, fields=sorted(data))
                s.commit()
            return new

    def save_master_version(self, candidate_id: str, name: str = "", actor: str = "user") -> ResumeVersion:
        skills = [sk for sk in self.skills(candidate_id, "approved") if sk.active]
        items = self.items(candidate_id, "approved")
        if not skills and not items:
            raise ValueError("Apruebe al menos un skill o un dato del perfil antes de guardar la versión maestra.")
        evidence = self.evidence_map(candidate_id)
        with self.db.session() as s:
            cand = s.get(CandidateProfile, candidate_id)
            last = s.scalar(select(func.max(ResumeVersion.version)).where(
                ResumeVersion.candidate_id == candidate_id, ResumeVersion.kind == "master")) or 0
            content = {
                "candidate": cand.name,
                "skills": [{"name": sk.name, "category": sk.category, "aliases": sk.aliases, "tag": sk.tag,
                            "weight": sk.weight, "proficiency": sk.proficiency, "years": sk.years, "notes": sk.notes,
                            "evidence": [e.excerpt for e in evidence.get(sk.id, [])][:3]} for sk in skills],
                "items": [{"kind": it.kind, "data": it.data} for it in items],
            }
            sources = sorted({e.source_id for evs in evidence.values() for e in evs if e.source_id}
                             | {it.source_id for it in items if it.source_id})
            ver = ResumeVersion(candidate_id=candidate_id, kind="master", version=last + 1,
                                name=(name.strip() or f"Maestro v{last + 1}")[:200], content=content,
                                provenance={"candidate_version": cand.version, "source_ids": sources,
                                            "approved_by": actor},
                                approved=True, approved_at=utcnow())
            s.add(ver)
            s.flush()
            self._audit(s, candidate_id, "master_version_saved", actor, version=ver.version, skills=len(skills),
                        items=len(items))
            s.commit()
            return ver

    def versions(self, candidate_id: str) -> list[ResumeVersion]:
        with self.db.session() as s:
            return list(s.scalars(select(ResumeVersion).where(ResumeVersion.candidate_id == candidate_id)
                                  .order_by(ResumeVersion.created_at.desc())))

    # ------------------------------------------------------------------ matching
    def build_matcher(self, s: Session, mode_id: str) -> SkillMatcher | None:
        cid = self.active_candidate_id(s, mode_id)
        if not cid:
            return None
        cand = s.get(CandidateProfile, cid)
        rows = list(s.scalars(select(Skill).where(Skill.candidate_id == cid)))
        approved = [sk for sk in rows if sk.status == "approved" and sk.active]
        if not approved:
            return None
        first_ev: dict[str, str] = {}
        for ev in s.scalars(select(SkillEvidence).where(SkillEvidence.skill_id.in_([sk.id for sk in approved]))
                            .order_by(SkillEvidence.created_at)):
            first_ev.setdefault(ev.skill_id, ev.excerpt)
        return SkillMatcher(
            candidate_id=cid, candidate_name=cand.name, candidate_version=cand.version,
            approved=[ApprovedSkill(sk.id, sk.name, sk.vocab_key, tuple(sk.aliases or ()), sk.weight,
                                    first_ev.get(sk.id, ""), sk.notes) for sk in approved],
            proposed_keys={sk.vocab_key for sk in rows if sk.status == "proposed" and sk.vocab_key},
            rejected_keys={sk.vocab_key for sk in rows if sk.status == "rejected" and sk.vocab_key},
        )

    def _reevaluate(self, candidate_id: str) -> None:
        for mode_id in self.modes_using(candidate_id):
            self.app.jobs.reevaluate_mode(mode_id)

    @staticmethod
    def _audit(s: Session, entity_id: str, action: str, actor: str, entity_type: str = "candidate",
               **details) -> None:
        s.add(AuditEvent(entity_type=entity_type, entity_id=entity_id, action=action, mode_id="", actor=actor,
                         details=details))
