"""Smoke test for the packaged portable app (Windows and macOS).

Usage:  python packaging/smoke_test.py dist/JobSearchManager-<platform>.zip

1. Extracts the release zip into a temp folder whose name contains a space and accents.
2. Starts the executable with a minimal environment (no Python on PATH) and --no-browser.
3. Checks /jsh-health and the main page, and that data/ was created next to the executable.
4. Checks that a second launch detects the running instance.
5. Stops the app, MOVES the folder, restarts it and checks the data is still there.

Exit code 0 = OK. This script needs Python to run, but the app under test does not.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

IS_WIN = sys.platform == "win32"
EXE_NAME = "JobSearchManager.exe" if IS_WIN else "JobSearchManager"
PORT = 8799


def extract(zip_path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        shutil.unpack_archive(str(zip_path), str(dest))
    else:
        # ditto keeps symlinks and executable bits (zipfile does not)
        subprocess.run(["ditto", "-x", "-k", str(zip_path), str(dest)], check=True)
    folder = dest / "JobSearchManager"
    if not (folder / EXE_NAME).exists():
        raise SystemExit(f"Executable not found after extraction: {folder / EXE_NAME}")
    return folder


def minimal_env() -> dict[str, str]:
    env = {"HOME": os.environ.get("HOME", ""), "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    if IS_WIN:
        windir = os.environ.get("SystemRoot", r"C:\Windows")
        env = {k: os.environ[k] for k in ("SystemRoot", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA")
               if k in os.environ}
        env["PATH"] = f"{windir}\\System32;{windir}"
    else:
        env["PATH"] = "/usr/bin:/bin"
    return env


def get(url: str, timeout: float = 3.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def wait_health(port: int, seconds: int = 90) -> dict:
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            return json.loads(get(f"http://127.0.0.1:{port}/jsh-health"))
        except Exception as exc:  # not up yet
            last = exc
            time.sleep(1)
    raise SystemExit(f"App did not answer on port {port} within {seconds}s: {last}")


def start(folder: Path, log_file: Path) -> subprocess.Popen:
    log = open(log_file, "ab")
    return subprocess.Popen([str(folder / EXE_NAME), "--no-browser", "--port", str(PORT)], cwd=str(folder),
                            env=minimal_env(), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def main() -> None:
    zip_path = Path(sys.argv[1]).resolve()
    root = Path(tempfile.mkdtemp(prefix="jsh smoke "))
    log_file = root / "app-output.log"
    proc = None
    try:
        folder = extract(zip_path, root / "Carpeta movida ñ")
        check = subprocess.run([str(folder / EXE_NAME), "--self-check"], cwd=str(folder), env=minimal_env(),
                               capture_output=True, timeout=180)
        assert check.returncode == 0 and b"SELF-CHECK OK" in check.stdout, (check.stdout[-2000:],
                                                                             check.stderr[-2000:])
        proc = start(folder, log_file)
        health = wait_health(PORT)
        assert health.get("app") == "job-search-harness", health
        assert Path(health["home"]).resolve() == folder.resolve(), (health["home"], folder)
        page = get(f"http://127.0.0.1:{PORT}/", timeout=10)
        assert "Job Search Harness" in page, "main page did not render"
        db = folder / "data" / "jobsearch.db"
        assert db.exists(), "data/jobsearch.db not created next to the executable"
        for rel in ("source/jsh/excel/csv_import.py", "source/jsh/ui/app.py", "INSTRUCCIONES.md", "AGENT_GUIDE.md"):
            assert (folder / rel).exists(), f"missing from the distribution: {rel}"
        if not IS_WIN:
            assert os.access(folder / "Iniciar JobSearchManager.command", os.X_OK), "launcher not executable"

        second = subprocess.run([str(folder / EXE_NAME), "--no-browser", "--port", str(PORT)], cwd=str(folder),
                                env=minimal_env(), capture_output=True, timeout=60)
        assert b"ya est" in second.stdout, second.stdout[-500:]

        stop(proc)
        proc = None
        con = sqlite3.connect(db)
        con.execute("INSERT INTO settings(key, value) VALUES ('smoke_marker', '\"ok\"')")
        con.commit()
        con.close()

        moved = root / "otra ubicación" / "JobSearchManager"
        moved.parent.mkdir(parents=True)
        shutil.move(str(folder), str(moved))
        proc = start(moved, log_file)
        health = wait_health(PORT)
        assert Path(health["home"]).resolve() == moved.resolve(), health
        con = sqlite3.connect(moved / "data" / "jobsearch.db")
        marker = con.execute("SELECT value FROM settings WHERE key='smoke_marker'").fetchone()
        con.close()
        assert marker and json.loads(marker[0]) == "ok", "data did not survive moving the folder"
        print(f"SMOKE TEST OK on {sys.platform}: {zip_path.name}")
    except BaseException:
        if log_file.exists():
            print("---- app output ----")
            print(log_file.read_text(errors="replace")[-4000:])
        raise
    finally:
        if proc is not None:
            stop(proc)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
