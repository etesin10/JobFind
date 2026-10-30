"""Database engine, versioned migrations, seeding and backups.

Migration strategy (documented deviation from Alembic, see master doc §19):
SQLite ``PRAGMA user_version`` stores the schema version. ``MIGRATIONS`` is an ordered
list of plain functions; before upgrading an existing non-empty database a consistent
copy is written to ``backups/`` (rollback = restore that file).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from .modes import LEGACY_KEYWORD_RULE_NAME, MODE_DEFS, default_rules, skills_match_rules
from .models import AuditEvent, Base, Mode, PromptTemplate, Rule, RuleVersion, SearchProfile, Setting
from .paths import AppPaths

log = logging.getLogger(__name__)


def _migration_1(engine: Engine) -> None:
    # NOTE: creates the CURRENT metadata, so later migrations must be idempotent (check before altering).
    Base.metadata.create_all(engine)


def _migration_2(engine: Engine) -> None:
    """Phase 2: candidate/résumé tables, skills-match columns and skills-match rank rules."""
    Base.metadata.create_all(engine)  # creates only missing tables
    cols = {c["name"] for c in inspect(engine).get_columns("job_mode_evaluations")}
    with engine.begin() as conn:
        if "skills_match" not in cols:
            conn.execute(text("ALTER TABLE job_mode_evaluations ADD COLUMN skills_match FLOAT"))
        if "skills_detail" not in cols:
            conn.execute(text("ALTER TABLE job_mode_evaluations ADD COLUMN skills_detail JSON"))
    with Session(engine) as s:
        for prof in s.scalars(select(SearchProfile)):
            changed = False
            cond = Rule.profile_id == prof.id
            rules = list(s.scalars(select(Rule).where(Rule.mode_id == prof.mode_id,
                                                      or_(cond, Rule.profile_id.is_(None)) if prof.is_default
                                                      else cond)))
            if not any(r.field == "skills_match_pct" for r in rules):
                changed = True
                for spec in skills_match_rules(prof.mode_id):
                    rule = Rule(profile_id=prof.id, **spec)
                    s.add(rule)
                    s.flush()
                    s.add(RuleVersion(rule_id=rule.id, version=1, change="created", snapshot=rule_snapshot(rule)))
                    s.add(AuditEvent(entity_type="rule", entity_id=rule.id, action="rule_created",
                                     mode_id=prof.mode_id, actor="migration_v2", details={"name": rule.name}))
            for r in rules:
                # Only an UNMODIFIED seed keyword rule is disabled; user-edited rules are left untouched.
                if r.name == LEGACY_KEYWORD_RULE_NAME and r.version == 1 and r.enabled and r.field == "description":
                    changed = True
                    r.enabled = False
                    r.version += 1
                    s.add(RuleVersion(rule_id=r.id, version=r.version, change="updated",
                                      snapshot={**rule_snapshot(r), "note": "Reemplazada por 'Skills del CV'"}))
                    s.add(AuditEvent(entity_type="rule", entity_id=r.id, action="rule_updated",
                                     mode_id=r.mode_id, actor="migration_v2",
                                     details={"name": r.name, "enabled": False,
                                              "reason": "Reemplazada por coincidencia con skills aprobados"}))
            if changed:
                prof.version += 1
        s.commit()


def _migration_3(engine: Engine) -> None:
    """Phase 3: company_sources (discovery watchlist) and connector_runs. Creates missing tables only."""
    Base.metadata.create_all(engine)


V_JOB_STATUS = """
CREATE VIEW v_job_status AS
SELECT j.id AS job_id, m.id AS mode_id, j.title, j.company_name, j.location_text, j.job_country, j.work_mode,
       j.canonical_url, j.apply_url, j.ad_status,
       (SELECT s.provider FROM job_sources s WHERE s.job_id = j.id ORDER BY s.retrieved_at LIMIT 1) AS source,
       (SELECT s.source_job_id FROM job_sources s WHERE s.job_id = j.id ORDER BY s.retrieved_at LIMIT 1)
           AS source_job_id,
       COALESCE(e.eligibility_status, 'unevaluated') AS eligibility_status, e.technical_score, e.skills_match,
       COALESCE(e.user_decision, 'new') AS user_decision, COALESCE(e.discard_reason, '') AS discard_reason,
       COALESCE(e.discard_note, '') AS discard_note, COALESCE(e.override, '') AS override,
       COALESCE(a.status, '') AS application_status, a.date_applied, a.next_followup,
       j.notes, j.pinned, j.updated_at
FROM jobs j CROSS JOIN modes m
LEFT JOIN job_mode_evaluations e ON e.job_id = j.id AND e.mode_id = m.id
LEFT JOIN applications a ON a.job_id = j.id AND a.mode_id = m.id
"""


def _migration_4(engine: Engine) -> None:
    """External-agent integration: job_inbox, evaluation facts_hash and the read-only v_job_status view."""
    Base.metadata.create_all(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("job_mode_evaluations")}
    with engine.begin() as conn:
        if "facts_hash" not in cols:
            conn.execute(text("ALTER TABLE job_mode_evaluations ADD COLUMN facts_hash VARCHAR(64) NOT NULL DEFAULT ''"))
        conn.execute(text("DROP VIEW IF EXISTS v_job_status"))
        conn.execute(text(V_JOB_STATUS))


V5_COLUMNS = {
    "companies": [("careers_url", "VARCHAR(500) NOT NULL DEFAULT ''"), ("watch", "BOOLEAN NOT NULL DEFAULT 0"),
                  ("legitimacy", "VARCHAR(20) NOT NULL DEFAULT 'unknown'"), ("evidence", "TEXT NOT NULL DEFAULT ''")],
    "contacts": [("phone", "VARCHAR(60) NOT NULL DEFAULT ''"), ("source", "VARCHAR(60) NOT NULL DEFAULT 'user'"),
                 ("updated_at", "DATETIME")],
    "candidate_profiles": [("contact", "JSON NOT NULL DEFAULT '{}'")],
    "job_mode_evaluations": [("ai_review", "JSON")],
}


def _migration_5(engine: Engine) -> None:
    """Phase 4/5 + management: contact_events, prompt_templates, ai_packages, ai_reviews, tailoring_runs and new
    columns on companies/contacts/candidate_profiles/job_mode_evaluations."""
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in V5_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _migration_6(engine: Engine) -> None:
    """Vacancy deletion tombstones (deleted_jobs) and contacts.established."""
    Base.metadata.create_all(engine)
    have = {c["name"] for c in inspect(engine).get_columns("contacts")}
    if "established" not in have:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE contacts ADD COLUMN established BOOLEAN NOT NULL DEFAULT 0"))


def _migration_7(engine: Engine) -> None:
    """Organisation & insights: job_tags, saved_views, fx_rates, job_liveness (new tables only)."""
    Base.metadata.create_all(engine)


MIGRATIONS: list[Callable[[Engine], None]] = [_migration_1, _migration_2, _migration_3, _migration_4, _migration_5,
                                              _migration_6, _migration_7]
SCHEMA_VERSION = len(MIGRATIONS)
BUSY_TIMEOUT_S = 30  # an external agent may hold short write transactions; wait instead of failing


def make_engine(db_file: Path) -> Engine:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}", future=True,
                           connect_args={"timeout": BUSY_TIMEOUT_S})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def enable_wal(engine: Engine) -> str:
    """WAL lets readers (this UI) and one writer (e.g. an external agent) work at the same time.

    Persistent in the DB file. Set ``JSH_SQLITE_JOURNAL=DELETE`` to opt out (e.g. data on a network
    share, where SQLite WAL is not supported). Returns the effective journal mode."""
    wanted = os.environ.get("JSH_SQLITE_JOURNAL", "WAL").strip().upper() or "WAL"
    if wanted not in ("WAL", "DELETE", "TRUNCATE"):
        wanted = "WAL"
    with engine.connect() as conn:
        mode = str(conn.exec_driver_sql(f"PRAGMA journal_mode={wanted}").scalar() or "").lower()
    if mode != wanted.lower():
        log.warning("SQLite journal_mode %s requested but %s is active", wanted, mode)
    return mode


def backup_database(db_file: Path, backups_dir: Path, label: str = "manual") -> Path | None:
    """Consistent SQLite online backup; returns the backup path (None if no DB yet).

    The copy is switched to a rollback journal so it is a single self-contained file."""
    if not db_file.exists():
        return None
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = backups_dir / f"jobsearch_{stamp}_{label}.db"
    n = 1
    while target.exists():  # the Windows clock can repeat a timestamp for back-to-back backups: never overwrite
        target = backups_dir / f"jobsearch_{stamp}-{n}_{label}.db"
        n += 1
    src = sqlite3.connect(db_file, timeout=BUSY_TIMEOUT_S)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()
    return target


def current_schema_version(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("PRAGMA user_version")).scalar() or 0)


def migrate(engine: Engine, paths: AppPaths | None = None) -> int:
    version = current_schema_version(engine)
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"Database schema v{version} is newer than this app (v{SCHEMA_VERSION}). "
                           "Use a newer JobSearchManager build.")
    if version < SCHEMA_VERSION and version > 0 and paths is not None:
        backup_database(paths.db_file, paths.backups, f"pre_migration_v{version}")
    for idx in range(version, SCHEMA_VERSION):
        log.info("Applying migration %s", idx + 1)
        MIGRATIONS[idx](engine)
        with engine.begin() as conn:
            conn.execute(text(f"PRAGMA user_version = {idx + 1}"))
    return SCHEMA_VERSION


def seed(session: Session) -> None:
    """Insert modes, default profiles and default rules once (never re-applied over user edits)."""
    for mdef in MODE_DEFS.values():
        if session.get(Mode, mdef.id) is None:
            session.add(Mode(id=mdef.id, name=mdef.name, description=mdef.description,
                             config={"workbook": mdef.workbook_name}))
    session.flush()
    if session.get(Setting, "rules_seeded") is None:
        profiles: dict[str, SearchProfile] = {}
        for mode_id in MODE_DEFS:
            prof = SearchProfile(mode_id=mode_id, name="Perfil principal", is_default=True)
            session.add(prof)
            profiles[mode_id] = prof
        session.flush()
        for spec in default_rules():
            rule = Rule(profile_id=profiles[spec["mode_id"]].id, **spec)
            session.add(rule)
            session.flush()
            session.add(RuleVersion(rule_id=rule.id, version=1, change="created", snapshot=rule_snapshot(rule)))
        session.add(Setting(key="rules_seeded", value=True))
    if session.get(Setting, "current_mode") is None:
        session.add(Setting(key="current_mode", value="CANADA_VISA"))
    from .prompts import DEFAULT_TEMPLATES  # local import: prompts has no DB dependency
    have = set(session.scalars(select(PromptTemplate.key)))
    for tpl in DEFAULT_TEMPLATES:
        if tpl["key"] not in have:  # never re-applied over user edits
            session.add(PromptTemplate(**tpl))
    session.commit()


def rule_snapshot(rule: Rule) -> dict:
    return {c: getattr(rule, c) for c in ("mode_id", "name", "enabled", "scope", "field", "operator", "value",
                                          "value_type", "logic_group", "group_operator", "action", "weight",
                                          "severity", "unknown_handling", "explanation", "priority", "version")}


class Database:
    """Owns the engine and session factory for one portable data folder."""

    def __init__(self, paths: AppPaths):
        self.paths = paths.ensure()
        self.engine = make_engine(paths.db_file)
        self.journal_mode = enable_wal(self.engine)
        migrate(self.engine, paths)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        with self.Session() as s:
            seed(s)

    def session(self) -> Session:
        return self.Session()

    def get_setting(self, key: str, default=None):
        with self.Session() as s:
            row = s.get(Setting, key)
            return default if row is None else row.value

    def set_setting(self, key: str, value) -> None:
        with self.Session() as s:
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value))
            else:
                row.value = value
            s.commit()

    def backup(self, label: str = "manual") -> Path | None:
        path = backup_database(self.paths.db_file, self.paths.backups, label)
        self.prune_backups()
        return path

    def prune_backups(self, keep_auto: int | None = None) -> int:
        """Keep the newest ``backup_keep`` (default 40) AUTOMATIC backups; manual and pre_restore copies are kept."""
        keep = keep_auto if keep_auto is not None else int(self.get_setting("backup_keep", 40) or 40)
        auto = [p for p in self.list_backups() if not p.stem.endswith(("_manual", "_pre_restore"))]
        removed = 0
        for old in auto[max(keep, 1):]:
            try:
                old.unlink()
                removed += 1
            except OSError:  # pragma: no cover - locked by antivirus/sync tool: try again next time
                pass
        return removed

    def list_backups(self) -> list[Path]:
        return sorted(self.paths.backups.glob("*.db"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)

    def inspect_backup(self, backup_file: Path) -> dict:
        """Validate a backup file without modifying anything. Raises ValueError if unusable."""
        backup_file = Path(backup_file).resolve()
        if backup_file.parent != self.paths.backups.resolve() or backup_file.suffix.lower() != ".db":
            raise ValueError("Solo se pueden restaurar archivos .db de la carpeta backups.")
        if not backup_file.is_file():
            raise ValueError("El archivo de backup no existe.")
        con = sqlite3.connect(backup_file)
        try:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            jobs = con.execute("SELECT count(*) FROM jobs").fetchone()[0] if "jobs" in tables else 0
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"El archivo no es una base de datos válida: {exc}") from exc
        finally:
            con.close()
        if integrity != "ok":
            raise ValueError(f"El backup está dañado (integrity_check: {integrity}).")
        if version > SCHEMA_VERSION:
            raise ValueError(f"El backup es de una versión más nueva de la app (esquema v{version}).")
        if "jobs" not in tables:
            raise ValueError("El archivo no es un backup de Job Search Harness.")
        return {"file": backup_file, "schema_version": version, "jobs": jobs,
                "size_kb": backup_file.stat().st_size // 1024}

    def restore(self, backup_file: Path) -> Path | None:
        """Replace the live database with a validated backup. The current DB is backed up first."""
        info = self.inspect_backup(backup_file)
        safety = self.backup("pre_restore")
        self.engine.dispose()
        src = sqlite3.connect(info["file"])
        dst = sqlite3.connect(self.paths.db_file, timeout=BUSY_TIMEOUT_S)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        self.engine.dispose()
        self.journal_mode = enable_wal(self.engine)
        migrate(self.engine, None)  # bring older backups up to the current schema
        with self.Session() as s:
            seed(s)
        log.info("Database restored from %s (safety copy: %s)", info["file"].name, safety)
        return safety

    def table_counts(self) -> dict[str, int]:
        with self.Session() as s:
            return {t.name: s.execute(select(text("count(*)")).select_from(t)).scalar() or 0
                    for t in Base.metadata.sorted_tables}

    def dispose(self) -> None:
        # Fold the WAL into the main file so a closed data folder is one self-contained .db (copy/USB safe).
        try:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # pragma: no cover - best effort (e.g. agent still writing)
            log.info("WAL checkpoint skipped: %s", exc)
        self.engine.dispose()
