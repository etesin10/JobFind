"""Application entry point: portable paths → DB → local UI on 127.0.0.1."""
from __future__ import annotations

import argparse
import logging
import multiprocessing
import platform
import re
import socket
import sys
import webbrowser
from logging.handlers import RotatingFileHandler

import httpx

from . import __version__
from .paths import PortableHomeError, check_writable, get_paths

APP_ID = "job-search-harness"
DEFAULT_PORT = 8765
_SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)(\s*[=:]\s*)\S+")


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _SECRET.sub(r"\1\2***", str(record.msg))
        return True


def setup_logging(logs_dir) -> None:
    handler = RotatingFileHandler(logs_dir / "jsh.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def port_free(port: int) -> bool:
    # On Windows a bind() test alone can succeed on a port in use (SO_REUSEADDR), so probe connect first.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":
            # macOS/Linux: without SO_REUSEADDR, TIME_WAIT connections from a just-closed instance make
            # bind() fail, and a quick relaunch would silently move to another port (uvicorn sets it too).
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def running_instance(port: int) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/jsh-health", timeout=1.5)
        return r.status_code == 200 and r.json().get("app") == APP_ID
    except Exception:
        return False


def _fatal(message: str) -> None:
    """Show a blocking error. Launched by double-click, the console would otherwise close immediately."""
    print("\n*** Job Search Harness no puede iniciar ***\n")
    print(message)
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\nPresione Enter para cerrar…")
    except (EOFError, OSError):
        pass
    sys.exit(2)


def _run_sources_headless(paths) -> int:
    """Run every enabled discovery source once and print a summary. Exit 0 if none failed."""
    port_in_use = not port_free(DEFAULT_PORT) and running_instance(DEFAULT_PORT)
    if port_in_use:
        print("Aviso: la app está abierta; las nuevas vacantes aparecerán al refrescar la página.")
    from .services import AppServices
    services = AppServices(paths)
    failed = 0
    try:
        sources = [s for s in services.discovery.list_sources() if s.enabled]
        if not sources:
            print("No hay búsquedas activas en 'Descubrir'.")
            return 0
        for src in sources:
            r = services.discovery.run_source(src.id, actor="scheduler")
            failed += r.status == "error"
            print(f"[{r.status.upper():7}] {r.source_name}: {r.fetched} traídas, {r.created} nuevas, "
                  f"{r.updated} actualizadas, {r.unchanged} sin cambios" + (f" — {r.error}" if r.error else ""))
    finally:
        services.close()
    sys.stdout.flush()
    return 1 if failed else 0


def _sync_headless(paths) -> int:
    """Process the external-agent inbox and reconcile direct DB edits once, then exit (0 = no rejected rows)."""
    from .services import AppServices
    services = AppServices(paths)
    try:
        summary = services.sync.tick(force=True)
        if summary is None:
            print("La base está ocupada; reintente en unos segundos.")
            return 1
        inbox = summary["inbox"]
        print(f"Bandeja: {inbox['created']} nuevas, {inbox['updated']} actualizadas, {inbox['unchanged']} sin cambios, "
              f"{inbox['error']} rechazadas · Cambios directos: {summary['external_jobs']} · "
              f"Reparadas: {summary['repaired']} · Re-evaluadas: {summary['reevaluated']}")
        for err in services.sync.inbox_stats()["errors"] if inbox["error"] else []:
            print(f"  [#{err['id']}] {err['message']}")
    finally:
        services.close()
    sys.stdout.flush()
    return 1 if summary and summary["inbox"]["error"] else 0


def _check_ads_headless(paths) -> int:
    """Check whether saved job URLs are still open (flags only; never changes or deletes anything)."""
    from .services import AppServices
    services = AppServices(paths)
    try:
        res = services.liveness.run_batch(interval_hours=1)
        print(f"Anuncios verificados: {res['checked']} · posiblemente cerrados: {res['likely_closed']} · "
              f"abiertos: {res['live']} · sin determinar: {res['unknown']}")
        for f in services.liveness.flagged():
            print(f"  ⚠ {f['title']} — {f['company_name']}: {f['reason']}")
    finally:
        services.close()
    sys.stdout.flush()
    return 0


def _mcp(paths, agent_name: str, read_only: bool) -> int:
    """Serve MCP over stdio. Nothing else may be written to stdout."""
    from .mcp_server import build_server
    from .services import AppServices
    log = logging.getLogger("jsh.mcp")
    services = AppServices(paths)
    try:
        log.info("MCP server starting (agent=%s, read_only=%s, home=%s)", agent_name, read_only, paths.home)
        build_server(services, agent_name, read_only).run("stdio")
        return 0
    except KeyboardInterrupt:  # pragma: no cover - client closed the pipe
        return 0
    finally:
        services.close()


def main(argv: list[str] | None = None) -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Job Search Harness")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador automáticamente")
    parser.add_argument("--home", help="Carpeta portátil de datos (por defecto: junto al ejecutable)")
    parser.add_argument("--self-check", action="store_true",
                        help="Verificar lectura de CV (DOCX/PDF) y SQLite en una carpeta temporal y salir")
    parser.add_argument("--run-sources", action="store_true",
                        help="Ejecutar todas las búsquedas activas de 'Descubrir' sin abrir la interfaz y salir "
                             "(para programarlo con el Programador de tareas / cron)")
    parser.add_argument("--sync", action="store_true",
                        help="Procesar la bandeja del agente externo (job_inbox) y los cambios directos en la base, "
                             "sin abrir la interfaz, y salir")
    parser.add_argument("--mcp", action="store_true",
                        help="Servidor MCP por stdio para agentes locales (Claude Desktop/Code, VS Code…); "
                             "no abre la interfaz")
    parser.add_argument("--check-ads", action="store_true",
                        help="Verificar si los anuncios guardados siguen abiertos (solo marca; no borra) y salir")
    parser.add_argument("--read-only", action="store_true", help="Con --mcp: exponer solo herramientas de lectura")
    parser.add_argument("--agent-name", default="mcp", help="Con --mcp: nombre del agente en el historial")
    args = parser.parse_args(argv)

    if args.self_check:
        from .selfcheck import run
        sys.exit(run())

    paths = get_paths(args.home)
    try:
        check_writable(paths.home)
        paths.ensure()
    except PortableHomeError as exc:
        if args.mcp:  # stdout belongs to the MCP protocol: report on stderr and exit
            print(str(exc), file=sys.stderr)
            sys.exit(2)
        _fatal(str(exc))
    setup_logging(paths.logs)
    log = logging.getLogger("jsh")
    log.info("Platform: %s %s (%s), Python %s", platform.system(), platform.release(), platform.machine(),
             platform.python_version())

    if args.mcp:
        sys.exit(_mcp(paths, args.agent_name, args.read_only))
    if args.run_sources:
        sys.exit(_run_sources_headless(paths))
    if args.sync:
        sys.exit(_sync_headless(paths))
    if args.check_ads:
        sys.exit(_check_ads_headless(paths))

    port = args.port
    if not port_free(port):
        if running_instance(port):
            print(f"Job Search Harness ya está abierto en http://127.0.0.1:{port}")
            if not args.no_browser:
                webbrowser.open(f"http://127.0.0.1:{port}")
            return
        port = next(p for p in range(port + 1, port + 50) if port_free(p))

    from nicegui import app, ui

    from .api import create_api, ensure_tokens
    from .services import AppServices
    from .ui.app import configure, index, quasar_language, start_background_sync

    services = AppServices(paths)
    configure(services)
    ensure_tokens(services)
    services.api_port = port
    app.mount("/api/v1", create_api(services))

    @app.get("/jsh-health")
    def health():
        return {"app": APP_ID, "version": __version__, "home": str(paths.home)}

    app.on_startup(lambda: start_background_sync(services))
    app.on_shutdown(services.close)
    log.info("Starting Job Search Harness %s at http://127.0.0.1:%s (home=%s)", __version__, port, paths.home)
    print(f"Job Search Harness {__version__} — http://127.0.0.1:{port}  (datos: {paths.home})")
    print("Cierre esta ventana (o presione Ctrl+C) para detener la aplicación.")
    ui.run(index, host="127.0.0.1", port=port, title="Job Search Harness", reload=False,
           show=not args.no_browser, language=quasar_language(services), show_welcome_message=False, favicon="💼")


if __name__ in {"__main__", "__mp_main__"}:
    main(sys.argv[1:])
