"""External-agent synchronisation (the DB may be written by another process).

Two supported write paths for an external agent (contract: INSTRUCCIONES.md §"Agente externo"):

1. ``job_inbox`` (recommended): ``INSERT INTO job_inbox(payload) VALUES ('{json}')``. The app validates,
   de-duplicates (same rules as every other import), evaluates, audits and records the outcome.
2. Direct ``UPDATE jobs SET ...`` of advertisement facts. ``reconcile`` repairs malformed values,
   re-evaluates jobs whose facts changed (``facts_hash``), adds a JD snapshot and bumps ``row_version``.

``tick`` runs both. The UI calls it every few seconds in the background; ``--sync`` runs it headless.
SQLite ``PRAGMA data_version`` (on a dedicated connection) tells whether anything committed since the
last pass, so an idle database costs one cheap PRAGMA per tick.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from ..db import BUSY_TIMEOUT_S
from ..models import AuditEvent, Job, JobModeEvaluation, JobSnapshot, utcnow
from ..modes import MODE_DEFS
from ..normalize import canonicalize_url, normalize_country, normalize_country_list, normalize_work_mode
from ..tolerant import parse_bool, parse_date, parse_datetime, parse_float, parse_int, parse_json
from .jobs import AD_STATUSES, DeletedJobError, JobInput, facts_hash, jd_hash

if TYPE_CHECKING:  # pragma: no cover
    from . import AppServices

log = logging.getLogger(__name__)

MAX_PAYLOAD_CHARS = 2_000_000
MAX_DESCRIPTION_CHARS = 200_000
MAX_AGENT_CHARS = 20_000
CHUNK = 200  # jobs per write transaction during reconcile (keeps the write lock short for the agent)
INBOX_KEEP_DAYS = 30
INPUT_KEYS = set(JobInput.model_fields)
CONTROL_KEYS = {"mode_id", "modes", "mode_data", "agent", "ad_status", "type"}
AGENT_ACTOR_PREFIXES = ("agent:", "api:", "mcp:", "external")
# banner group -> audit actions ('xxx_' = prefix)
AGENT_EVENT_GROUPS = {
    "external_jobs": ("job_external_insert", "job_external_update"),
    "created": ("job_created",),
    "updated": ("job_updated", "ad_status_changed", "mode_data_updated"),
    "reviews": ("ai_review_received",),
    "tailoring": ("tailoring_proposed",),
    "crm": ("company_", "contact_"),
    "skills": ("skills_proposed",),
}
WORK_MODES = ("", "remote", "hybrid", "onsite")
_DATE_OK = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DT_OK = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d{1,6})?$")
TEXT_COLS = ("company_name", "title", "job_country", "region", "location_text", "work_mode", "seniority",
             "employment_type", "currency", "salary_period", "canonical_url", "apply_url", "ad_status",
             "sponsorship_statement", "language", "description", "notes")
DT_COLS = {"last_checked": True, "created_at": False, "updated_at": False}  # name -> nullable
EMPTY_FACT = ("", None, "unknown")


def _dt_text(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _channel(actor: str) -> str:
    """'api:x' → 'api', 'mcp:x' → 'mcp', anything else → 'inbox'."""
    head = actor.split(":", 1)[0]
    return head if head in ("api", "mcp") else "inbox"


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(x) for x in e['loc']) or 'payload'}: {e['msg']}" for e in exc.errors()[:8]]
        return "Datos inválidos — " + "; ".join(parts)
    if isinstance(exc, json.JSONDecodeError):
        return f"El payload no es JSON válido ({exc.msg}, posición {exc.pos})"
    return f"{type(exc).__name__}: {exc}"[:1000]


class SyncService:
    def __init__(self, app: "AppServices", interval_s: float = 10.0):
        self.app = app
        self.db = app.db
        self.jobs = app.jobs
        self.interval_s = interval_s
        self.seq = 0  # increments when external changes became visible (the UI compares it)
        self.last_change: dict[str, Any] = {}
        self.history: list[tuple[int, dict[str, Any]]] = []
        self.last_tick: datetime | None = None
        self.last_error = ""
        self._lock = threading.Lock()
        self._watch: sqlite3.Connection | None = None
        self._last_dv: int | None = None
        self._last_purge = 0.0
        self._last_audit: int | None = None
        try:
            self._agent_events()  # baseline: older events are not "new" for this session
        except Exception:  # pragma: no cover - a locked DB at startup only delays the baseline
            self._last_audit = None

    # ------------------------------------------------------------------ change detection
    def _data_version(self) -> int:
        if self._watch is None:
            self._watch = sqlite3.connect(self.db.paths.db_file, timeout=BUSY_TIMEOUT_S, check_same_thread=False)
        return int(self._watch.execute("PRAGMA data_version").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            if self._watch is not None:
                self._watch.close()
                self._watch = None

    def reset_after_wipe(self) -> None:
        """After "wipe all": audit rowids restart, so re-baseline (otherwise new agent events would be ignored)."""
        self._last_audit = None
        self._last_dv = None
        self.history = []
        self.last_change = {}
        try:
            self._agent_events()
        except Exception:  # pragma: no cover
            self._last_audit = None

    def changes_since(self, seq: int) -> dict[str, Any]:
        """Aggregate of the visible external changes after ``seq`` (for the UI banner)."""
        out = {"created": 0, "updated": 0, "error": 0, "external_jobs": 0, "reviews": 0, "tailoring": 0, "crm": 0, "skills": 0,
               "at": ""}
        for s, ch in self.history:
            if s > seq:
                out["error"] += ch["inbox"].get("error", 0)
                for k in ("created", "updated", "external_jobs", "reviews", "tailoring", "crm", "skills"):
                    out[k] += ch.get("agent_events", {}).get(k, 0)
                out["at"] = ch.get("at", "")
        return out

    def _agent_events(self) -> dict[str, int]:
        """Counts of audit events written by agents (inbox, REST API, MCP, direct DB edits) since the last call."""
        out = {k: 0 for k in AGENT_EVENT_GROUPS}
        with self.db.engine.connect() as conn:
            if self._last_audit is None:
                self._last_audit = int(conn.execute(text("SELECT coalesce(max(rowid), 0) FROM audit_events"))
                                       .scalar() or 0)
                return out
            rows = conn.execute(text("SELECT rowid, action, actor FROM audit_events WHERE rowid > :r ORDER BY rowid"),
                                {"r": self._last_audit}).all()
        for rowid, action, actor in rows:
            self._last_audit = max(self._last_audit, int(rowid))
            if not str(actor or "").startswith(AGENT_ACTOR_PREFIXES):
                continue
            for group, actions in AGENT_EVENT_GROUPS.items():
                if action in actions or any(action.startswith(p) for p in actions if p.endswith("_")):
                    out[group] += 1
                    break
        return out

    def tick(self, force: bool = False) -> dict[str, Any] | None:
        """One sync pass. Returns a summary, or None if another pass is already running."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            summary: dict[str, Any] = {"inbox": self.ingest_inbox(), "repaired": 0, "reevaluated": 0,
                                       "external_jobs": 0}
            dv = self._data_version()
            if force or dv != self._last_dv:
                # Remember the version seen BEFORE scanning: a commit racing with the scan triggers another pass.
                self._last_dv = dv
                try:
                    summary.update(self.reconcile())
                except Exception:
                    self._last_dv = None  # retry on the next tick instead of silently skipping
                    raise
            if time.monotonic() - self._last_purge > 6 * 3600:
                self._last_purge = time.monotonic()
                summary["purged"] = self.purge_inbox()
            summary["agent_events"] = self._agent_events()
            visible = summary["inbox"]["error"] + sum(summary["agent_events"].values())
            if visible:
                self.seq += 1
                self.last_change = {**summary, "at": datetime.now().strftime("%H:%M:%S")}
                self.history = [*self.history[-99:], (self.seq, self.last_change)]
                log.info("External sync: %s", summary)
            self.last_tick = datetime.now()
            self.last_error = ""
            return summary
        except OperationalError as exc:  # e.g. the agent held a write lock longer than the busy timeout
            self.last_error = str(exc)
            log.warning("Sync pass postponed: %s", exc)
            return None
        except Exception as exc:  # visible in Configuración; the next tick retries
            self.last_error = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("Sync pass failed")
            return None
        finally:
            self._lock.release()

    # ------------------------------------------------------------------ inbox
    def ingest_inbox(self, limit: int = 500) -> dict[str, int]:
        counts = {"created": 0, "updated": 0, "unchanged": 0, "error": 0}
        with self.db.engine.connect() as conn:
            rows = conn.execute(text("SELECT id, source, payload FROM job_inbox WHERE status = 'pending' "
                                     "ORDER BY id LIMIT :n"), {"n": limit}).all()
        for rid, source, payload in rows:
            src = (str(source).strip() if source is not None else "")[:60] or "agent"
            try:
                job_id, result, warnings = self.ingest_payload(payload, src)
                status, message = "imported", " | ".join(warnings)
            except OperationalError:
                raise  # database busy/locked: leave the row pending for the next pass
            except Exception as exc:
                job_id, result, status, message = "", "", "error", _error_text(exc)
                log.info("Inbox row %s rejected: %s", rid, message)
            counts[result or "error"] += 1
            with self.db.engine.begin() as conn:
                conn.execute(text("UPDATE job_inbox SET status = :st, result = :r, job_id = :j, message = :m, "
                                  "processed_at = :t WHERE id = :id"),
                             {"st": status, "r": result, "j": job_id, "m": message[:4000],
                              "t": _dt_text(utcnow()), "id": rid})
        return counts

    def _ingest_other(self, kind: str, data: dict[str, Any], source: str, actor: str) -> tuple[str, str, list[str]]:
        """Inbox rows with ``"type"``: company | contact | contact_event | review | tailoring."""
        if kind == "company":
            cid, status = self.app.crm.upsert_company(data, actor=actor, fill_only=True)
            return cid, status, []
        if kind == "contact":
            cid, status = self.app.crm.upsert_contact(data, actor=actor, fill_only=True)
            return cid, status, []
        if kind == "contact_event":
            eid = self.app.crm.add_event(str(data.get("contact_id") or ""), str(data.get("kind") or "note"),
                                         str(data.get("note") or ""), data.get("happened_on"),
                                         str(data.get("job_id") or ""), str(data.get("mode_id") or ""), actor)
            return eid, "created", []
        if kind == "review":
            rep = self.app.reviews.import_result(data, source=_channel(actor), actor=actor)
            msgs = [f"{r['job_id']}: {r['reason']}" for r in rep["rejected"]] + rep["warnings"]
            if not rep["received"]:
                raise ValueError("Ninguna revisión aceptada: " + "; ".join(msgs)[:1000])
            return rep["received"][0]["review_id"], "created", msgs
        if kind == "tailoring":
            rep = self.app.tailoring.import_proposal(data, source=_channel(actor), actor=actor)
            return rep["run_id"], "created", [f"{r['change_id']}: {r['reason']}" for r in rep["rejected"]] \
                + rep["warnings"]
        if kind == "skills":
            rep = self.app.tailoring.propose_skills(str(data.get("mode") or ""), data.get("skills"), actor)
            msgs = [f"Ya existían: {', '.join(rep['existing'])[:300]}"] if rep["existing"] else []
            return "", "created" if rep["proposed"] else "unchanged", msgs
        raise ValueError(f"type desconocido: {kind!r} (job, company, contact, contact_event, review, tailoring, "
                         "skills)")

    def ingest_payload(self, payload: Any, source: str = "agent", actor: str | None = None
                       ) -> tuple[str, str, list[str]]:
        """Validate + upsert one agent payload. Returns (job_id, created|updated|unchanged, warnings)."""
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        if isinstance(payload, str):
            if len(payload) > MAX_PAYLOAD_CHARS:
                raise ValueError(f"Payload demasiado grande (> {MAX_PAYLOAD_CHARS:,} caracteres)")
            data = json.loads(payload)
        else:
            data = payload
        if not isinstance(data, dict):
            raise ValueError('El payload debe ser un objeto JSON: {"title": ..., "company_name": ..., ...}')
        kind = str(data.get("type") or "job").strip().lower()
        if kind != "job":
            return self._ingest_other(kind, {k: v for k, v in data.items() if k != "type"}, source,
                                      (actor or f"agent:{source}")[:60])
        warnings: list[str] = []
        fields = {k: v for k, v in data.items() if k in INPUT_KEYS}
        extra = dict(fields.get("extra")) if isinstance(fields.get("extra"), dict) else {}
        unknown = sorted(k for k in data if k not in INPUT_KEYS and k not in CONTROL_KEYS)
        if unknown:
            extra["agent_fields"] = {k: data[k] for k in unknown}
            warnings.append(f"Campos no reconocidos guardados en extra.agent_fields: {', '.join(unknown)[:300]}")
        agent = data.get("agent")
        if agent is not None:
            if not isinstance(agent, dict):
                warnings.append("'agent' debe ser un objeto; se ignoró")
            else:
                agent = dict(agent)
                if "score" in agent:
                    score = parse_float(agent["score"])
                    if score is None or not 0 <= score <= 100:
                        warnings.append(f"agent.score inválido ({agent['score']!r}); debe ser 0-100")
                        agent.pop("score")
                    else:
                        agent["score"] = score
                if len(json.dumps(agent, default=str)) > MAX_AGENT_CHARS:
                    agent = {k: agent[k] for k in ("score", "recommendation", "summary") if k in agent}
                    agent["summary"] = str(agent.get("summary", ""))[:4000]
                    warnings.append("'agent' demasiado grande; se guardaron solo score/recommendation/summary")
                extra["agent"] = {**agent, "source": source, "received_at": utcnow().isoformat(timespec="seconds")}
        fields["extra"] = extra
        desc = fields.get("description")
        if isinstance(desc, str) and len(desc) > MAX_DESCRIPTION_CHARS:
            fields["description"] = desc[:MAX_DESCRIPTION_CHARS]
            warnings.append("JD truncada a 200.000 caracteres")
        if not fields.get("source"):
            fields["source"] = source
        # Agents send real-world formats ("October 29, 2026", "95k"): normalise; drop with a warning
        # (never reject the whole job) when a non-essential field cannot be interpreted.
        if fields.get("posted_date") not in (None, ""):
            d = parse_date(fields["posted_date"])
            if d is None:
                warnings.append(f"posted_date no reconocida ({str(fields.pop('posted_date'))[:40]!r}); se omitió")
            else:
                fields["posted_date"] = d.isoformat()
        for key in ("salary_min", "salary_max"):
            if fields.get(key) not in (None, ""):
                f = parse_float(fields[key])
                if f is None:
                    warnings.append(f"{key} no numérico ({str(fields.pop(key))[:40]!r}); se omitió")
                else:
                    fields[key] = f
        for key in ("canonical_url", "apply_url"):
            if fields.get(key):
                try:
                    canonicalize_url(str(fields[key]))
                except ValueError:
                    warnings.append(f"{key} no es http/https ({str(fields.pop(key))[:60]!r}); se omitió")
        inp = JobInput(**fields)

        raw_modes = data.get("modes", data.get("mode_id"))
        modes = [raw_modes] if isinstance(raw_modes, str) else (raw_modes if isinstance(raw_modes, list) else [])
        bad = [m for m in modes if m not in MODE_DEFS]
        if bad:
            warnings.append(f"Modos desconocidos ignorados: {', '.join(map(str, bad))[:200]}")
        actor = (actor or f"agent:{source}")[:60]
        try:
            job, result = self.jobs.upsert_job(inp, [m for m in modes if m in MODE_DEFS], actor=actor)
        except DeletedJobError as exc:  # not an error for the agent: the user removed it on purpose
            return "", "unchanged", [*warnings, f"Omitida: {exc}"]

        status = data.get("ad_status")
        if status not in (None, ""):
            if status in AD_STATUSES:
                if self.jobs.set_ad_status(job.id, status, actor=actor) and result == "unchanged":
                    result = "updated"
            else:
                warnings.append(f"ad_status inválido ({status!r}); opciones: {', '.join(AD_STATUSES)}")
        mode_data = data.get("mode_data")
        if mode_data is not None:
            if not isinstance(mode_data, dict):
                warnings.append("'mode_data' debe ser {\"MODO\": {\"campo\": valor}}; se ignoró")
            else:
                for mode_id, values in mode_data.items():
                    if mode_id not in MODE_DEFS or not isinstance(values, dict):
                        warnings.append(f"mode_data.{mode_id} ignorado (modo desconocido o no es objeto)")
                        continue
                    filled, msg = self._fill_mode_data(job.id, mode_id, values, actor)
                    if msg:
                        warnings.append(msg)
                    if filled and result == "unchanged":
                        result = "updated"
        return job.id, result, warnings

    def _fill_mode_data(self, job_id: str, mode_id: str, values: dict[str, Any], actor: str) -> tuple[bool, str]:
        """Agents only FILL empty/unknown mode facts; values already set (e.g. by the user) are kept."""
        allowed = {f.key for f in MODE_DEFS[mode_id].mode_fields}
        with self.db.session() as s:
            ev = s.scalar(select(JobModeEvaluation).where(JobModeEvaluation.job_id == job_id,
                                                          JobModeEvaluation.mode_id == mode_id))
            current = (ev.mode_data if ev and isinstance(ev.mode_data, dict) else {}) or {}
        unknown = [k for k in values if k not in allowed]
        kept = [k for k in values if k in allowed and current.get(k) not in EMPTY_FACT and current.get(k) != values[k]]
        todo = {k: v for k, v in values.items() if k in allowed and current.get(k) in EMPTY_FACT
                and v not in EMPTY_FACT}
        msgs = []
        if unknown:
            msgs.append(f"mode_data.{mode_id}: campos desconocidos {', '.join(unknown)[:200]}")
        if kept:
            msgs.append(f"mode_data.{mode_id}: se conservaron valores existentes de {', '.join(kept)[:200]}")
        if todo:
            try:
                self.jobs.update_mode_data(job_id, mode_id, todo, actor=actor)
            except ValueError as exc:
                msgs.append(f"mode_data.{mode_id}: {exc}")
                return False, "; ".join(msgs)
        return bool(todo), "; ".join(msgs)

    def purge_inbox(self, keep_days: int = INBOX_KEEP_DAYS) -> int:
        """Delete successfully imported inbox rows older than ``keep_days`` (errors are kept for review)."""
        cutoff = _dt_text(utcnow() - timedelta(days=keep_days))
        with self.db.engine.begin() as conn:
            return conn.execute(text("DELETE FROM job_inbox WHERE status = 'imported' AND processed_at < :c"),
                                {"c": cutoff}).rowcount or 0

    def inbox_stats(self, recent_errors: int = 10) -> dict[str, Any]:
        with self.db.engine.connect() as conn:
            counts = dict(conn.execute(text("SELECT status, count(*) FROM job_inbox GROUP BY status")).all())
            errors = conn.execute(text("SELECT id, source, created_at, message, substr(payload, 1, 160) "
                                       "FROM job_inbox WHERE status = 'error' ORDER BY id DESC LIMIT :n"),
                                  {"n": recent_errors}).all()
        return {"pending": counts.get("pending", 0), "imported": counts.get("imported", 0),
                "error": counts.get("error", 0),
                "errors": [{"id": r[0], "source": r[1], "created_at": str(r[2] or ""), "message": r[3] or "",
                            "payload": r[4] or ""} for r in errors],
                "last_tick": self.last_tick.strftime("%H:%M:%S") if self.last_tick else "",
                "last_error": self.last_error, "journal_mode": self.db.journal_mode}

    def retry_errors(self) -> int:
        with self.db.engine.begin() as conn:
            return conn.execute(text("UPDATE job_inbox SET status = 'pending', message = '' "
                                     "WHERE status = 'error'")).rowcount or 0

    # ------------------------------------------------------------------ direct edits
    def repair_jobs(self) -> int:
        """Rewrite malformed values written directly into ``jobs`` in canonical form. Returns jobs fixed."""
        with self.db.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(text("SELECT * FROM jobs"))]

        def row_fix(r: dict[str, Any]) -> dict[str, Any]:
            fix: dict[str, Any] = {}
            for c in TEXT_COLS:
                v = r.get(c)
                if v is not None and not isinstance(v, str):
                    fix[c] = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
            countries = parse_json(r.get("remote_eligible_countries"))
            if not (isinstance(countries, list) and all(isinstance(x, str) for x in countries)):
                fix["remote_eligible_countries"] = json.dumps(
                    normalize_country_list(countries if isinstance(countries, (list, str)) else str(countries or "")))
            tags = parse_json(r.get("mode_tags"))
            if not (isinstance(tags, list) and all(t in MODE_DEFS for t in tags)):
                fix["mode_tags"] = json.dumps([m for m in MODE_DEFS if m in str(tags or "")])
            extra = parse_json(r.get("extra"))
            if not isinstance(extra, dict):
                fix["extra"] = json.dumps({} if extra in (None, "") else {"external_raw": extra}, default=str)
            pd = r.get("posted_date")
            if pd is not None and not (isinstance(pd, str) and _DATE_OK.match(pd) and parse_date(pd)):
                d = parse_date(pd)
                fix["posted_date"] = d.isoformat() if d else None
            for c, nullable in DT_COLS.items():
                v = r.get(c)
                if (v is None and nullable) or (isinstance(v, str) and _DT_OK.match(v) and parse_datetime(v)):
                    continue
                dt = parse_datetime(v)
                fix[c] = _dt_text(dt) if dt else (None if nullable else _dt_text(utcnow()))
            for c in ("salary_min", "salary_max"):
                v = r.get(c)
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                    fix[c] = parse_float(v)
            rv = r.get("row_version")
            if isinstance(rv, bool) or not isinstance(rv, int):
                fix["row_version"] = parse_int(rv) or 1
            if r.get("pinned") not in (0, 1):
                fix["pinned"] = int(bool(parse_bool(r.get("pinned"))))
            wm = fix.get("work_mode", r.get("work_mode"))
            if isinstance(wm, str) and wm not in WORK_MODES and normalize_work_mode(wm) in WORK_MODES:
                fix["work_mode"] = normalize_work_mode(wm)
            jc = fix.get("job_country", r.get("job_country"))
            if isinstance(jc, str) and jc and normalize_country(jc) and normalize_country(jc) != jc:
                fix["job_country"] = normalize_country(jc)
            st = fix.get("ad_status", r.get("ad_status"))
            if st not in AD_STATUSES:
                fix["ad_status"] = "unknown"
            for c in ("canonical_url", "apply_url"):
                v = fix.get(c, r.get(c))
                if isinstance(v, str) and v:
                    try:
                        canonicalize_url(v)
                    except ValueError:  # e.g. javascript: — never let it become a clickable link
                        fix[c] = ""
            return fix

        fixes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for r in rows:
            try:
                fix = row_fix(r)
            except Exception:  # one pathological row must not block the others
                log.exception("Could not inspect job %s for repair", r.get("id"))
                continue
            if fix:
                fixes[str(r["id"])] = (fix, {c: r.get(c) for c in fix})
        if not fixes:
            return 0
        done: dict[str, list[str]] = {}
        with self.db.engine.begin() as conn:
            for job_id, (fix, old) in fixes.items():
                sets = ", ".join(f"{c} = :{c}" for c in fix)  # column names come only from fixed lists above
                # Only if the agent has not changed those values again since we read them.
                guard = " AND ".join(f"{c} IS :old_{c}" for c in fix)
                params = {**fix, **{f"old_{c}": v for c, v in old.items()}, "_id": job_id}
                if conn.execute(text(f"UPDATE jobs SET {sets} WHERE id = :_id AND {guard}"), params).rowcount:
                    done[job_id] = sorted(fix)
        if not done:
            return 0
        with self.db.session() as s:
            for job_id, fields in done.items():
                s.add(AuditEvent(entity_type="job", entity_id=job_id[:32], action="job_repaired", actor="sync",
                                 details={"fields": fields}))
            s.commit()
        log.warning("Repaired malformed external values in %d job(s)", len(done))
        return len(done)

    def _stale(self, s, ids: list[str]) -> list[tuple[Job, list[str], bool, bool]]:
        """(job, stale modes, evaluation missing, changed-by-someone-else) for jobs whose facts changed."""
        jobs = list(s.scalars(select(Job).where(Job.id.in_(ids))))
        evs = {(e.job_id, e.mode_id): e for e in s.scalars(
            select(JobModeEvaluation).where(JobModeEvaluation.job_id.in_(ids)))}
        out = []
        for job in jobs:
            h = facts_hash(job)
            stale = [m for m in MODE_DEFS if evs.get((job.id, m)) is None or evs[(job.id, m)].facts_hash != h]
            if stale:
                missing = any(evs.get((job.id, m)) is None for m in stale)
                # Empty stored hash = evaluated before schema v4 (silent refresh, not an external change).
                out.append((job, stale, missing, missing or any(evs[(job.id, m)].facts_hash for m in stale)))
        return out

    def reconcile(self) -> dict[str, int]:
        """Repair, then evaluate every job whose facts changed since its last evaluation."""
        repaired = self.repair_jobs()
        reevaluated = external = 0
        with self.db.session() as s:
            job_ids = list(s.scalars(select(Job.id)))
        candidates: list[str] = []
        for start in range(0, len(job_ids), CHUNK):  # cheap read-only pass (no write lock)
            with self.db.session() as s:
                candidates += [j.id for j, *_ in self._stale(s, job_ids[start:start + CHUNK])]
        for start in range(0, len(candidates), CHUNK):
            with self.db.session() as s:
                # Re-check under the write lock: nobody can commit between this read and our write, so the
                # app's own concurrent writes are never mistaken for external edits and bumps are not lost.
                s.connection().exec_driver_sql("BEGIN IMMEDIATE")
                cache: dict[str, tuple] = {}
                for job, stale, missing, changed in self._stale(s, candidates[start:start + CHUNK]):
                    try:
                        if changed:
                            external += 1
                            self._absorb_external(s, job, new=missing)
                        for m in stale:
                            if m not in cache:
                                cache[m] = (self.jobs._rules(s, m), self.jobs.matcher_for(s, m))
                            self.jobs._evaluate(s, job, m, *cache[m])
                            reevaluated += 1
                    except Exception:  # one odd job must not block the rest
                        log.exception("Could not re-evaluate job %s", job.id)
                s.commit()
        return {"repaired": repaired, "reevaluated": reevaluated, "external_jobs": external}

    def _absorb_external(self, s, job: Job, new: bool) -> None:
        if not new:
            self.jobs._bump(job)  # Excel conflict detection relies on row_version
        if job.company_name and not job.company_id:
            comp = self.jobs._company(s, job.company_name)
            job.company_id = comp.id if comp else None
        if job.description:
            h = jd_hash(job.description)
            if s.scalar(select(JobSnapshot).where(JobSnapshot.job_id == job.id, JobSnapshot.jd_hash == h)) is None:
                s.add(JobSnapshot(job_id=job.id, jd_text=job.description, jd_hash=h, source_url=job.canonical_url))
        s.add(AuditEvent(entity_type="job", entity_id=job.id, action="job_external_insert" if new
                         else "job_external_update", actor="external", details={"title": job.title[:200]}))
