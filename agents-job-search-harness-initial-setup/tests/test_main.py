import logging
import socket

from jsh.main import RedactFilter, port_free
from jsh.paths import get_paths


def test_port_free_detects_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert port_free(port) is False


def test_log_redaction():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "calling with api_key=sk-123 and token: abc", None, None)
    RedactFilter().filter(rec)
    assert "sk-123" not in rec.msg and "abc" not in rec.msg


def test_paths_relative_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("JSH_HOME", str(tmp_path / "X"))
    p = get_paths().ensure()
    assert p.db_file == (tmp_path / "X" / "data" / "jobsearch.db").resolve()
    for d in ("data", "excel", "config", "resumes", "exports", "backups", "logs"):
        assert (tmp_path / "X" / d).is_dir()


def test_self_check_runs_in_temp_folder_only(capsys):
    from jsh.selfcheck import run
    assert run() == 0
    assert "SELF-CHECK OK" in capsys.readouterr().out

def test_run_sources_headless(tmp_path, capsys):
    import httpx
    from jsh.main import _run_sources_headless
    from jsh.paths import get_paths
    from jsh.services import AppServices
    from tests.test_discovery import AMAZON_JOB, amazon_handler
    paths = get_paths(tmp_path / "h").ensure()
    app = AppServices(paths)
    app.discovery.create_source({"name": "Amazon", "provider": "amazon_jobs", "countries": "CA"})
    app.close()
    import jsh.services.discovery as disc
    original = disc.DiscoveryService.__init__

    def patched(self, a):
        original(self, a)
        self.http_options = {"transport": httpx.MockTransport(amazon_handler({0: [AMAZON_JOB]}, [])),
                             "sleep": lambda s: None, "min_interval": 0}
    disc.DiscoveryService.__init__ = patched
    try:
        assert _run_sources_headless(paths) == 0
    finally:
        disc.DiscoveryService.__init__ = original
    out = capsys.readouterr().out
    assert "[OK     ] Amazon: 1 traídas, 1 nuevas" in out