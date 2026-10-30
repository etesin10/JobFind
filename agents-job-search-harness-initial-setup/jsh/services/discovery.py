"""EmployerDiscoveryService / JobSourceAdapter runner (Phase 3).

Guarantees (spec §6, §16.12):
- A connector failure is recorded in ``connector_runs`` and shown in the UI; it never deletes or changes
  existing jobs, decisions, notes or applications.
- Imported jobs go through the normal idempotent upsert (dedupe by provider + source id, then URL), so a
  re-run updates the ad but keeps the user's triage.
- Network fetching happens outside any DB transaction.
"""
from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import select

from ..connectors import CONNECTORS, ConnectorError, HttpClient, SourceQuery
from ..connectors.base import IDENTIFIER_RE
from ..models import AuditEvent, Company, CompanySource, ConnectorRun, utcnow
from ..modes import MODE_DEFS
from ..normalize import normalize_country
from .jobs import DeletedJobError

MAX_KEYWORDS = 10
SOURCE_FIELDS = ("name", "provider", "identifier", "keywords", "countries", "mode_id", "max_results", "enabled",
                 "notes")


class DiscoveryService:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.http_options: dict[str, Any] = {}  # tests inject transport / sleep here
        self._running: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ sources
    @staticmethod
    def providers() -> dict[str, Any]:
        return CONNECTORS

    def list_sources(self) -> list[CompanySource]:
        with self.db.session() as s:
            return list(s.scalars(select(CompanySource).order_by(CompanySource.created_at)))

    def get_source(self, source_id: str) -> CompanySource | None:
        with self.db.session() as s:
            return s.get(CompanySource, source_id)

    def _clean(self, data: dict[str, Any], current: CompanySource | None = None) -> dict[str, Any]:
        merged = {k: getattr(current, k) for k in SOURCE_FIELDS} if current else {}
        merged.update({k: v for k, v in data.items() if k in SOURCE_FIELDS})
        provider = merged.get("provider")
        if provider not in CONNECTORS:
            raise ValueError("Fuente no compatible. Opciones: " + ", ".join(CONNECTORS))
        conn = CONNECTORS[provider]
        name = str(merged.get("name") or "").strip()
        if not name or len(name) > 200:
            raise ValueError("La búsqueda necesita un nombre (máx. 200 caracteres).")
        ident = str(merged.get("identifier") or "").strip()
        if conn.needs_identifier and not IDENTIFIER_RE.match(ident):
            raise ValueError(f"{conn.label}: indique el identificador del tablero del empleador "
                             "(solo letras, números, - _ .), p. ej. 'gitlab'.")
        if not conn.needs_identifier:
            ident = ""
        raw_kw = merged.get("keywords") or []
        kws = raw_kw if isinstance(raw_kw, (list, tuple)) else str(raw_kw).replace(";", ",").split(",")
        keywords = []
        for k in kws:
            k = " ".join(str(k).split())
            if k and len(k) <= 60 and k.lower() not in (x.lower() for x in keywords):
                keywords.append(k)
        if len(keywords) > MAX_KEYWORDS:
            raise ValueError(f"Máximo {MAX_KEYWORDS} palabras clave por búsqueda.")
        raw_c = merged.get("countries") or []
        cs = raw_c if isinstance(raw_c, (list, tuple)) else str(raw_c).replace(";", ",").split(",")
        countries = []
        for c in cs:
            code = normalize_country(str(c).strip())
            if not code:
                continue
            if len(code) != 2 or not code.isalpha():
                raise ValueError(f"País no reconocido: '{c}'. Use códigos de 2 letras (CA, MX, US…).")
            if code not in countries:
                countries.append(code)
        mode_id = str(merged.get("mode_id") or "")
        if mode_id and mode_id not in MODE_DEFS:
            raise ValueError(f"Modo inválido: {mode_id}")
        try:
            max_results = int(merged.get("max_results") or 100)
        except (TypeError, ValueError) as exc:
            raise ValueError("Máximo de resultados debe ser un número.") from exc
        cap = 500 if provider == "amazon_jobs" else 1000
        if not 1 <= max_results <= cap:
            raise ValueError(f"Máximo de resultados entre 1 y {cap} para esta fuente.")
        enabled = merged.get("enabled", True)
        return {"name": name, "provider": provider, "identifier": ident, "keywords": keywords,
                "countries": countries, "mode_id": mode_id, "max_results": max_results,
                "enabled": enabled if isinstance(enabled, bool) else str(enabled).lower() in ("1", "true", "yes"),
                "notes": str(merged.get("notes") or "")[:1000]}

    def create_source(self, data: dict[str, Any], actor: str = "user") -> CompanySource:
        clean = self._clean(data)
        with self.db.session() as s:
            src = CompanySource(**clean)
            s.add(src)
            s.flush()
            s.add(AuditEvent(entity_type="source", entity_id=src.id, action="source_created", actor=actor,
                             details={"name": src.name, "provider": src.provider}))
            s.commit()
            return src

    def update_source(self, source_id: str, data: dict[str, Any], actor: str = "user") -> CompanySource:
        with self.db.session() as s:
            src = s.get(CompanySource, source_id)
            if src is None:
                raise KeyError(source_id)
            clean = self._clean(data, src)
            changed = [k for k, v in clean.items() if getattr(src, k) != v]
            for k in changed:
                setattr(src, k, clean[k])
            if changed:
                s.add(AuditEvent(entity_type="source", entity_id=src.id, action="source_updated", actor=actor,
                                 details={"fields": changed}))
            s.commit()
            return src

    def delete_source(self, source_id: str, actor: str = "user") -> None:
        """Delete the saved search only. Jobs it imported (and all triage/history) are kept."""
        with self.db.session() as s:
            src = s.get(CompanySource, source_id)
            if src is None:
                return
            for run in s.scalars(select(ConnectorRun).where(ConnectorRun.source_id == source_id)):
                run.source_id = None
            s.add(AuditEvent(entity_type="source", entity_id=src.id, action="source_deleted", actor=actor,
                             details={"name": src.name}))
            s.delete(src)
            s.commit()

    # ------------------------------------------------------------------ runs
    def runs(self, limit: int = 50, source_id: str | None = None) -> list[ConnectorRun]:
        with self.db.session() as s:
            stmt = select(ConnectorRun).order_by(ConnectorRun.started_at.desc()).limit(limit)
            if source_id:
                stmt = stmt.where(ConnectorRun.source_id == source_id)
            return list(s.scalars(stmt))

    def run_source(self, source_id: str, actor: str = "user") -> ConnectorRun:
        with self._lock:
            if source_id in self._running:
                raise ValueError("Esta búsqueda ya se está ejecutando.")
            self._running.add(source_id)
        try:
            return self._run(source_id, actor)
        finally:
            with self._lock:
                self._running.discard(source_id)

    def run_all_enabled(self, actor: str = "user") -> list[ConnectorRun]:
        return [self.run_source(src.id, actor) for src in self.list_sources() if src.enabled]

    def _run(self, source_id: str, actor: str) -> ConnectorRun:
        with self.db.session() as s:
            src = s.get(CompanySource, source_id)
            if src is None:
                raise KeyError(source_id)
            run = ConnectorRun(source_id=src.id, provider=src.provider, source_name=src.name)
            s.add(run)
            s.commit()
            run_id = run.id
            query = SourceQuery(identifier=src.identifier, keywords=list(src.keywords or []),
                                countries=list(src.countries or []), max_results=src.max_results)
            provider, mode_id = src.provider, src.mode_id or None
        conn = CONNECTORS.get(provider)
        status, error, warnings, fetched, counts, job_ids = "ok", "", [], 0, {}, []
        total, requests = None, 0
        try:
            if conn is None:
                raise ConnectorError(f"Fuente '{provider}' no disponible en esta versión.")
            opts = {"min_interval": conn.min_interval, **self.http_options}
            with HttpClient(conn.hosts, **opts) as http:
                result = conn.fetch(query, http)
                requests = http.requests
            fetched, total, warnings = len(result.jobs), result.total_available, list(result.warnings)
            status = "partial" if result.partial else "ok"
            counts = {"created": 0, "updated": 0, "unchanged": 0}
            skipped_deleted = 0
            for inp in result.jobs:
                try:
                    job, st = self.app.jobs.upsert_job(inp, mode_id, actor=f"connector:{provider}")
                except DeletedJobError:
                    skipped_deleted += 1  # deleted by the user on purpose: never re-created
                    continue
                except Exception as exc:  # one bad record never aborts the run
                    warnings.append(f"No se guardó '{inp.title[:60]}': {type(exc).__name__}")
                    status = "partial"
                    continue
                counts[st] += 1
                if st != "unchanged":
                    job_ids.append(job.id)
            if skipped_deleted:
                warnings.append(f"{skipped_deleted} vacante(s) omitida(s) porque usted las eliminó.")
        except ConnectorError as exc:
            status, error = "error", str(exc)
        except Exception as exc:  # unexpected: record it, never crash the UI or touch existing data
            status, error = "error", f"Error inesperado del conector ({type(exc).__name__}). Ver logs/jsh.log."
            import logging
            logging.getLogger(__name__).exception("connector %s failed", provider)
        with self.db.session() as s:
            run = s.get(ConnectorRun, run_id)
            run.finished_at = utcnow()
            run.status, run.error, run.warnings = status, error[:2000], warnings[:50]
            run.fetched, run.total_available, run.requests = fetched, total, requests
            run.created, run.updated = counts.get("created", 0), counts.get("updated", 0)
            run.unchanged, run.job_ids = counts.get("unchanged", 0), job_ids[:1000]
            src = s.get(CompanySource, source_id)
            if src is not None:
                src.last_run_at, src.last_status = run.finished_at, status
                if src.company_id is None and job_ids:
                    from ..models import Job
                    first = s.get(Job, job_ids[0])
                    if first is not None and first.company_id:
                        src.company_id = first.company_id
                        comp = s.get(Company, first.company_id)
                        if comp is not None and not comp.ats_provider:
                            comp.ats_provider, comp.ats_board_id = provider, src.identifier
            s.add(AuditEvent(entity_type="source", entity_id=source_id, action=f"source_run_{status}", actor=actor,
                             details={"fetched": fetched, **counts, "error": error[:200]}))
            s.commit()
            return run
