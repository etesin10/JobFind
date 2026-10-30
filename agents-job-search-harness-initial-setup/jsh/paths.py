"""Portable path resolution (Windows and macOS).

All mutable data lives next to the executable (frozen build) or the project root
(source run), never inside PyInstaller's resource directory. ``JSH_HOME`` overrides
the base folder (used by tests and for custom portable locations).

macOS specifics:
- If the executable runs inside an ``.app`` bundle (``X.app/Contents/MacOS/X``), the
  portable home is the folder that CONTAINS the ``.app``. Writing inside the bundle would
  break its code signature.
- Gatekeeper "App Translocation" runs quarantined apps from a random read-only path
  (``/private/var/folders/.../AppTranslocation/...``). This is detected and reported
  instead of silently writing data somewhere else.
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DATA_DIRS = ("data", "excel", "config", "resumes", "exports", "backups", "logs")


class PortableHomeError(RuntimeError):
    """The portable folder cannot be used (read-only, translocated, ...)."""


HOME_POINTER = "jsh_home.txt"


def home_for_executable(executable: str | Path) -> Path:
    exe = Path(executable).resolve()
    parent = exe.parent
    # X.app/Contents/MacOS/X → folder containing X.app
    if parent.name == "MacOS" and parent.parent.name == "Contents" and parent.parent.parent.suffix == ".app":
        return parent.parent.parent.parent
    return parent


def follow_pointer(base: Path) -> Path:
    """Optional ``jsh_home.txt`` next to the app: first non-comment line = data folder.

    Relative paths are resolved against ``base``. This lets the Windows and macOS builds share ONE
    data folder, e.g. on a USB drive: ``../JobSearchData``. Use forward slashes for portability.
    """
    pointer = base / HOME_POINTER
    if not pointer.is_file():
        return base
    for line in pointer.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            target = Path(line.replace("\\", "/")).expanduser()
            return (target if target.is_absolute() else base / target).resolve()
    return base


def app_home() -> Path:
    env = os.environ.get("JSH_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return follow_pointer(home_for_executable(sys.executable))
    return follow_pointer(Path(__file__).resolve().parent.parent)


def is_translocated(path: Path) -> bool:
    return "AppTranslocation" in Path(path).parts


def check_writable(home: Path) -> None:
    """Raise PortableHomeError with a user-facing (Spanish) explanation if ``home`` is unusable."""
    if is_translocated(home):
        raise PortableHomeError(
            "macOS está ejecutando la app desde una ubicación temporal de solo lectura (App Translocation).\n"
            "Solución: mueva la carpeta JobSearchManager a Documentos o al Escritorio y, en Terminal, ejecute:\n"
            "  xattr -dr com.apple.quarantine <ruta de la carpeta JobSearchManager>\n"
            "Luego ábrala de nuevo con 'Iniciar JobSearchManager.command'.")
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=home, prefix=".jsh_write_test_", delete=True):
            pass
    except OSError as exc:
        raise PortableHomeError(
            f"No se puede escribir en la carpeta de la app: {home}\n({exc})\n"
            "Copie la carpeta JobSearchManager a una ubicación con permisos de escritura "
            "(Documentos, Escritorio o una memoria USB que no sea de solo lectura) o use --home <carpeta>.") from exc


@dataclass(frozen=True)
class AppPaths:
    home: Path

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def excel(self) -> Path:
        return self.home / "excel"

    @property
    def config(self) -> Path:
        return self.home / "config"

    @property
    def resumes(self) -> Path:
        return self.home / "resumes"

    @property
    def exports(self) -> Path:
        return self.home / "exports"

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def db_file(self) -> Path:
        return self.data / "jobsearch.db"

    def ensure(self) -> "AppPaths":
        for name in DATA_DIRS:
            (self.home / name).mkdir(parents=True, exist_ok=True)
        return self


def get_paths(home: Path | str | None = None) -> AppPaths:
    return AppPaths(Path(home).expanduser().resolve() if home else app_home())
