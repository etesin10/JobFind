"""Employers (companies) and contacts management — ContactManager (spec §4 'Empresas' / 'Contactos').

Agents (inbox, REST API, MCP) write with ``fill_only=True``: they can create records and fill EMPTY fields, but
never overwrite what the user wrote. Every change is audited.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import delete, func, or_, select

from ..models import Application, AuditEvent, Company, Contact, ContactEvent, Job, utcnow
from ..modes import MODE_DEFS
from ..normalize import canonicalize_url, normalize_name
from ..tolerant import parse_bool, parse_date

LEGITIMACY = {"unknown": "Sin verificar", "verified": "Verificada", "suspicious": "Sospechosa"}
COMPANY_TEXT = {"name": 200, "website": 500, "careers_url": 500, "ats_provider": 40, "ats_board_id": 200,
                "notes": 20_000, "evidence": 20_000}
CONTACT_TEXT = {"name": 200, "role": 200, "email": 200, "phone": 60, "profile_url": 500, "evidence": 20_000,
                "notes": 20_000}
URL_FIELDS = {"website", "careers_url", "profile_url"}
EVENT_KINDS = {"email": "Correo", "call": "Llamada", "message": "Mensaje", "meeting": "Reunión",
               "interview": "Entrevista", "note": "Nota"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean(data: dict[str, Any], spec: dict[str, int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, limit in spec.items():
        if key in data and data[key] is not None:
            v = str(data[key]).strip()[:limit]
            if key in URL_FIELDS and v:
                v = canonicalize_url(v)  # ValueError for non-http(s)
            if key == "email" and v and not _EMAIL.match(v):
                raise ValueError(f"Correo no válido: {v[:80]}")
            out[key] = v
    return out


class CrmService:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    @staticmethod
    def _audit(s, entity_type: str, entity_id: str, action: str, actor: str, **details) -> None:
        s.add(AuditEvent(entity_type=entity_type, entity_id=entity_id, action=action, actor=actor[:60],
                         details=details))

    @staticmethod
    def _user_fields(s, entity_id: str) -> set[str]:
        """Fields the USER has ever saved on this record (agents in fill_only mode must not touch them)."""
        out: set[str] = set()
        for details in s.scalars(select(AuditEvent.details).where(AuditEvent.entity_id == entity_id,
                                                                  AuditEvent.actor == "user")):
            if isinstance(details, dict):
                out.update(str(f) for f in details.get("fields") or [])
        return out

    # ------------------------------------------------------------------ companies
    def list_companies(self, query: str = "", watch_only: bool = False) -> list[dict[str, Any]]:
        with self.db.session() as s:
            stmt = select(Company)
            if watch_only:
                stmt = stmt.where(Company.watch.is_(True))
            companies = list(s.scalars(stmt.order_by(Company.name)))
            jobs = dict(s.execute(select(Job.company_id, func.count()).group_by(Job.company_id)).all())
            contacts = dict(s.execute(select(Contact.company_id, func.count()).group_by(Contact.company_id)).all())
        q = normalize_name(query)
        out = []
        for c in companies:
            row = self._company_dict(c)
            row.update(jobs=jobs.get(c.id, 0), contacts=contacts.get(c.id, 0))
            if q and q not in normalize_name(" ".join(str(v) for v in row.values())):
                continue
            out.append(row)
        return out

    @staticmethod
    def _company_dict(c: Company) -> dict[str, Any]:
        return {"company_id": c.id, "name": c.name, "website": c.website, "careers_url": c.careers_url,
                "ats_provider": c.ats_provider, "ats_board_id": c.ats_board_id, "watch": bool(c.watch),
                "legitimacy": c.legitimacy or "unknown", "evidence": c.evidence, "notes": c.notes,
                "updated_at": c.updated_at.isoformat(timespec="seconds") if c.updated_at else ""}

    def get_company(self, company_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            c = s.get(Company, company_id)
            if c is None:
                raise KeyError(company_id)
            out = self._company_dict(c)
            out["jobs"] = [{"job_id": j.id, "title": j.title, "location_text": j.location_text,
                            "ad_status": j.ad_status} for j in s.scalars(select(Job).where(Job.company_id == c.id)
                                                                       .order_by(Job.title))]
            out["contacts"] = [self._contact_dict(x, c.name) for x in s.scalars(
                select(Contact).where(Contact.company_id == c.id).order_by(Contact.name))]
        return out

    def upsert_company(self, data: dict[str, Any], actor: str = "user", fill_only: bool = False
                       ) -> tuple[str, str]:
        """Create or update a company (by ``company_id`` or name). Returns (company_id, created|updated|unchanged)."""
        fields = _clean(data, COMPANY_TEXT)
        if "watch" in data:
            fields["watch"] = bool(parse_bool(data["watch"]))
        if "legitimacy" in data and data["legitimacy"] not in (None, ""):
            if data["legitimacy"] not in LEGITIMACY:
                raise ValueError(f"legitimacy inválido (opciones: {', '.join(LEGITIMACY)})")
            fields["legitimacy"] = data["legitimacy"]
        with self.db.session() as s:
            comp = s.get(Company, str(data.get("company_id") or "")) if data.get("company_id") else None
            if comp is None and data.get("company_id"):
                raise KeyError(f"company_id no existe: {data['company_id']}")
            if comp is None:
                name = fields.get("name", "")
                if not name:
                    raise ValueError("Falta el nombre de la empresa (name).")
                comp = s.scalar(select(Company).where(Company.normalized_name == normalize_name(name)))
            status = "unchanged"
            if comp is None:
                comp = Company(name=fields["name"], normalized_name=normalize_name(fields["name"]))
                s.add(comp)
                status = "created"
            changed = []
            user_set = self._user_fields(s, comp.id) if fill_only and status != "created" else set()
            for k, v in fields.items():
                cur = getattr(comp, k)
                if k == "name" and status != "created":
                    continue  # renames only through the UI form (normalized_name must stay unique)
                if fill_only and status != "created":
                    # Agents only fill EMPTY fields; the watchlist flag is the user's choice; legitimacy only while
                    # the user never set it (an explicit 'unknown' by the user is a decision too).
                    if k == "watch" or k in user_set or cur not in ("", None, "unknown"):
                        continue
                if cur != v:
                    setattr(comp, k, v)
                    changed.append(k)
            if changed and status == "unchanged":
                status = "updated"
            if status != "unchanged":
                comp.updated_at = utcnow()
                s.flush()
                self._audit(s, "company", comp.id, f"company_{status}", actor, fields=changed, name=comp.name)
            s.commit()
            return comp.id, status

    def rename_company(self, company_id: str, name: str, actor: str = "user") -> None:
        name = name.strip()[:200]
        if not name:
            raise ValueError("El nombre no puede quedar vacío.")
        with self.db.session() as s:
            comp = s.get(Company, company_id)
            other = s.scalar(select(Company).where(Company.normalized_name == normalize_name(name),
                                                   Company.id != company_id))
            if other is not None:
                raise ValueError(f"Ya existe la empresa '{other.name}'.")
            old, comp.name, comp.normalized_name = comp.name, name, normalize_name(name)
            self._audit(s, "company", comp.id, "company_renamed", actor, previous=old, name=name)
            s.commit()

    def delete_company(self, company_id: str, actor: str = "user") -> None:
        with self.db.session() as s:
            comp = s.get(Company, company_id)
            if comp is None:
                return
            if s.scalar(select(func.count()).select_from(Job).where(Job.company_id == company_id)):
                raise ValueError("La empresa tiene vacantes; no se puede eliminar (puede desmarcarla de la lista).")
            for c in s.scalars(select(Contact).where(Contact.company_id == company_id)):
                c.company_id = None
            self._audit(s, "company", comp.id, "company_deleted", actor, name=comp.name)
            s.delete(comp)
            s.commit()

    # ------------------------------------------------------------------ contacts
    @staticmethod
    def _contact_dict(c: Contact, company_name: str = "") -> dict[str, Any]:
        return {"contact_id": c.id, "name": c.name, "role": c.role, "company_id": c.company_id or "",
                "company_name": company_name, "email": c.email, "phone": c.phone, "profile_url": c.profile_url,
                "evidence": c.evidence, "notes": c.notes, "source": c.source, "established": bool(c.established),
                "updated_at": (c.updated_at or c.created_at).isoformat(timespec="seconds")}

    def list_contacts(self, query: str = "", company_id: str = "", established: bool | None = None
                      ) -> list[dict[str, Any]]:
        with self.db.session() as s:
            stmt = select(Contact, Company.name).outerjoin(Company, Company.id == Contact.company_id)
            if company_id:
                stmt = stmt.where(Contact.company_id == company_id)
            if established is not None:
                stmt = stmt.where(Contact.established.is_(bool(established)))
            rows = s.execute(stmt.order_by(Contact.name)).all()
            last = dict(s.execute(select(ContactEvent.contact_id, func.max(ContactEvent.created_at))
                                  .group_by(ContactEvent.contact_id)).all())
        q = normalize_name(query)
        out = []
        for c, cname in rows:
            row = self._contact_dict(c, cname or "")
            row["last_interaction"] = last[c.id].isoformat(timespec="minutes") if last.get(c.id) else ""
            if q and q not in normalize_name(" ".join(str(v) for v in row.values())):
                continue
            out.append(row)
        return out

    def get_contact(self, contact_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            c = s.get(Contact, contact_id)
            if c is None:
                raise KeyError(contact_id)
            cname = s.get(Company, c.company_id).name if c.company_id else ""
            out = self._contact_dict(c, cname)
            titles = dict(s.execute(select(Job.id, Job.title)).all())
            out["events"] = [{"event_id": e.id, "kind": e.kind, "happened_on": e.happened_on.isoformat()
                              if e.happened_on else "", "note": e.note, "job_id": e.job_id or "",
                              "job_title": titles.get(e.job_id or "", ""), "mode_id": e.mode_id, "actor": e.actor,
                              "created_at": e.created_at.isoformat(timespec="minutes")}
                             for e in s.scalars(select(ContactEvent).where(ContactEvent.contact_id == contact_id)
                                                .order_by(ContactEvent.created_at.desc()))]
            out["applications"] = [{"job_id": a.job_id, "job_title": titles.get(a.job_id, ""), "mode_id": a.mode_id,
                                    "status": a.status} for a in s.scalars(
                select(Application).where(Application.contact_id == contact_id))]
        return out

    def upsert_contact(self, data: dict[str, Any], actor: str = "user", fill_only: bool = False
                       ) -> tuple[str, str]:
        """Create/update a contact. Dedupe: contact_id, then email, then name + company. Returns (id, status)."""
        fields = _clean(data, CONTACT_TEXT)
        if "established" in data and data["established"] is not None:
            flag = parse_bool(data["established"])
            if flag is None:
                raise ValueError("established debe ser true o false.")
            fields["established"] = flag
        company_id = str(data.get("company_id") or "")
        with self.db.session() as s:
            if not company_id and str(data.get("company_name") or "").strip():
                company_id = self.app.jobs._company(s, str(data["company_name"])[:200]).id
            if company_id and s.get(Company, company_id) is None:
                raise KeyError(f"company_id no existe: {company_id}")
            c = s.get(Contact, str(data["contact_id"])) if data.get("contact_id") else None
            if c is None and data.get("contact_id"):
                raise KeyError(f"contact_id no existe: {data['contact_id']}")
            if c is None and fields.get("email"):
                c = s.scalar(select(Contact).where(func.lower(Contact.email) == fields["email"].lower()))
            if c is None and fields.get("name"):
                stmt = select(Contact).where(func.lower(Contact.name) == fields["name"].lower())
                stmt = stmt.where(Contact.company_id == company_id) if company_id else \
                    stmt.where(Contact.company_id.is_(None))
                c = s.scalar(stmt)
            status = "unchanged"
            if c is None:
                if not fields.get("name"):
                    raise ValueError("Falta el nombre del contacto (name).")
                c = Contact(name=fields["name"], company_id=company_id or None, source=actor[:60])
                s.add(c)
                status = "created"
            changed = []
            user_set = self._user_fields(s, c.id) if fill_only and status != "created" else set()
            for k, v in fields.items():
                cur = getattr(c, k)
                if fill_only and status != "created":
                    # established is a yes/no flag: agents may set it only while the user never did.
                    if k in user_set or (k != "established" and cur not in ("", None)):
                        continue
                if cur != v:
                    setattr(c, k, v)
                    changed.append(k)
            if company_id and c.company_id != company_id and not (fill_only and c.company_id):
                c.company_id = company_id
                changed.append("company_id")
            if changed and status == "unchanged":
                status = "updated"
            if status != "unchanged":
                c.updated_at = utcnow()
                s.flush()
                self._audit(s, "contact", c.id, f"contact_{status}", actor, fields=changed, name=c.name)
            s.commit()
            return c.id, status

    def delete_contact(self, contact_id: str, actor: str = "user") -> None:
        with self.db.session() as s:
            c = s.get(Contact, contact_id)
            if c is None:
                return
            for a in s.scalars(select(Application).where(Application.contact_id == contact_id)):
                a.contact_id = None
            s.execute(delete(ContactEvent).where(ContactEvent.contact_id == contact_id))
            s.flush()
            self._audit(s, "contact", c.id, "contact_deleted", actor, name=c.name)
            s.delete(c)
            s.commit()

    def add_event(self, contact_id: str, kind: str = "note", note: str = "", happened_on: Any = None,
                  job_id: str = "", mode_id: str = "", actor: str = "user") -> str:
        if kind not in EVENT_KINDS:
            raise ValueError(f"Tipo de interacción inválido (opciones: {', '.join(EVENT_KINDS)})")
        if mode_id and mode_id not in MODE_DEFS:
            raise ValueError(f"Modo inválido: {mode_id}")
        when = parse_date(happened_on) if happened_on not in (None, "") else date.today()
        with self.db.session() as s:
            if s.get(Contact, contact_id) is None:
                raise KeyError(f"contact_id no existe: {contact_id}")
            if job_id and s.get(Job, job_id) is None:
                raise KeyError(f"job_id no existe: {job_id}")
            ev = ContactEvent(contact_id=contact_id, job_id=job_id or None, mode_id=mode_id, kind=kind,
                              happened_on=when, note=str(note or "")[:20_000], actor=actor[:60])
            s.add(ev)
            s.get(Contact, contact_id).updated_at = utcnow()
            s.flush()
            self._audit(s, "contact", contact_id, "contact_event", actor, kind=kind, job_id=job_id)
            s.commit()
            return ev.id

    def set_application_contact(self, job_id: str, mode_id: str, contact_id: str | None, actor: str = "user") -> None:
        """Link a contact to the job's application in a mode (creates the application record if needed)."""
        with self.db.session() as s:
            if contact_id and s.get(Contact, contact_id) is None:
                raise KeyError(contact_id)
            app = s.scalar(select(Application).where(Application.job_id == job_id, Application.mode_id == mode_id))
            if app is None:
                app = Application(job_id=job_id, mode_id=mode_id, status="discovered")
                s.add(app)
            app.contact_id = contact_id or None
            s.flush()
            self._audit(s, "application", app.id, "contact_linked", actor, job_id=job_id, contact_id=contact_id or "")
            s.commit()

    def job_contacts(self, job_id: str) -> list[dict[str, Any]]:
        """Contacts of the job's company plus contacts linked to its applications."""
        with self.db.session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            linked = {a.contact_id for a in s.scalars(select(Application).where(Application.job_id == job_id))
                      if a.contact_id}
            conds = [Contact.id.in_(linked)] if linked else []
            if job.company_id:
                conds.append(Contact.company_id == job.company_id)
            if not conds:
                return []
            return [self._contact_dict(c) for c in s.scalars(select(Contact).where(or_(*conds))
                                                               .order_by(Contact.name))]
