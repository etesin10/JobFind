"""Service container wiring one portable data folder to all services."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete

from ..db import Database, seed
from ..models import Base, Setting
from ..paths import AppPaths, get_paths
from .jobs import JobInput, JobService
from .rules_service import RuleService, RuleValidationError

log = logging.getLogger(__name__)

WIPE_CONFIRM_WORD = "BORRAR TODO"
# Interface preferences survive "wipe all" (they are not job-search data).
KEEP_SETTING_PREFIXES = ("ui_",)


class AppServices:
    def __init__(self, paths: AppPaths | None = None):
        from ..excel.csv_import import CsvImportService
        from ..excel.workbook import WorkbookSyncService
        from .candidate import CandidateService
        from .crm import CrmService
        from .discovery import DiscoveryService
        from .insights import InsightService
        from .jd import JDService
        from .liveness import LivenessService
        from .reviews import ReviewService
        from .salary import SalaryService
        from .suggestions import SuggestionService
        from .sync import SyncService
        from .tailoring import TailoringService
        from .views import ViewService

        self.paths = (paths or get_paths()).ensure()
        self.db = Database(self.paths)
        self.jobs = JobService(self.db)
        self.rules = RuleService(self.db, self.jobs)
        self.salary = SalaryService(self)
        self.jobs.salary_normalizer = self.salary.normalize_job
        self.candidate = CandidateService(self)
        self.jobs.matcher_factory = self.candidate.build_matcher
        self.discovery = DiscoveryService(self)
        self.suggestions = SuggestionService(self)
        self.workbooks = WorkbookSyncService(self)
        self.csv = CsvImportService(self)
        self.crm = CrmService(self)
        self.jd = JDService(self)
        self.reviews = ReviewService(self)
        self.tailoring = TailoringService(self)
        self.views = ViewService(self)
        self.insights = InsightService(self)
        self.liveness = LivenessService(self)
        self.sync = SyncService(self)

    @property
    def current_mode(self) -> str:
        return self.db.get_setting("current_mode", "CANADA_VISA")

    @current_mode.setter
    def current_mode(self, mode_id: str) -> None:
        self.db.set_setting("current_mode", mode_id)

    def wipe_all(self, keep_backup: bool = True, confirm: str = "") -> dict[str, Any]:
        """Erase ALL job-search data: database rows, résumé files, exports, Excel files, backups and logs.

        With ``keep_backup`` ONE final copy of the database is written first (``backups/…_pre_wipe.db``) and every
        other backup is removed. Interface preferences (``ui_*`` settings) are kept. Default modes, rules,
        profiles and prompt templates are re-created; API tokens are regenerated (agents need the new ones)."""
        if confirm != WIPE_CONFIRM_WORD:
            raise ValueError(f"Escriba exactamente «{WIPE_CONFIRM_WORD}» para confirmar.")
        report: dict[str, Any] = {"backup": "", "rows": 0, "files": 0, "locked": []}
        final = self.db.backup("pre_wipe") if keep_backup else None
        report["backup"] = str(final) if final else ""
        for b in self.db.list_backups():
            if final is not None and b.resolve() == final.resolve():
                continue
            self._unlink(b, report)
        with self.db.session() as s:
            kept = [(st.key, st.value) for st in s.query(Setting) if st.key.startswith(KEEP_SETTING_PREFIXES)]
            for table in reversed(Base.metadata.sorted_tables):
                report["rows"] += s.execute(delete(table)).rowcount or 0
            s.commit()
            for k, v in kept:
                s.add(Setting(key=k, value=v))
            s.commit()
        with self.db.session() as s:
            seed(s)
        try:
            with self.db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.exec_driver_sql("VACUUM")
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # pragma: no cover - an agent holding a read transaction: space is reused later
            log.info("VACUUM after wipe skipped: %s", exc)
        for folder in (self.paths.resumes, self.paths.exports, self.paths.excel):
            for f in sorted(folder.rglob("*"), reverse=True):
                if f.is_file():
                    self._unlink(f, report)
                elif f.is_dir():
                    try:
                        f.rmdir()
                    except OSError:
                        pass
        for f in self.paths.logs.glob("*"):
            if not f.is_file():
                continue
            if f.suffix == ".log":  # the open log: truncate in place (Windows cannot delete it)
                try:
                    with open(f, "w", encoding="utf-8"):
                        pass
                    continue
                except OSError:
                    pass
            self._unlink(f, report)
        from ..api import ensure_tokens
        ensure_tokens(self)
        self.salary.invalidate()
        self.sync.reset_after_wipe()
        log.info("All data wiped (rows=%s files=%s locked=%s)", report["rows"], report["files"], report["locked"])
        return report

    @staticmethod
    def _unlink(path, report: dict[str, Any]) -> None:
        try:
            path.unlink()
            report["files"] += 1
        except OSError:
            report["locked"].append(path.name)

    def close(self) -> None:
        self.sync.close()
        self.db.dispose()


__all__ = ["AppServices", "JobInput", "JobService", "RuleService", "RuleValidationError", "WIPE_CONFIRM_WORD"]
