"""Cross-platform portability tests (run on Windows and macOS CI runners)."""
import pathlib
import tempfile

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement

from jsh import paths as P
from jsh.excel.csv_import import read_csv_bytes, suggest_mapping
from jsh.main import main

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- portable home
def test_home_for_plain_folder_and_mac_app_bundle(tmp_path):
    plain = tmp_path / "JobSearchManager" / "JobSearchManager"
    assert P.home_for_executable(plain) == (tmp_path / "JobSearchManager").resolve()
    in_app = tmp_path / "Apps" / "JobSearchManager.app" / "Contents" / "MacOS" / "JobSearchManager"
    assert P.home_for_executable(in_app) == (tmp_path / "Apps").resolve()  # never inside the signed bundle


def test_home_pointer_shares_one_data_folder(tmp_path):
    win, mac, data = tmp_path / "JobSearchManager-Windows", tmp_path / "JobSearchManager-macOS", tmp_path / "Datos"
    for d in (win, mac):
        d.mkdir()
        (d / P.HOME_POINTER).write_text("# carpeta de datos compartida\n\n..\\Datos\n", encoding="utf-8")
    assert P.follow_pointer(win) == data.resolve() == P.follow_pointer(mac)
    (win / P.HOME_POINTER).write_text(str(data), encoding="utf-8")  # absolute path also works
    assert P.follow_pointer(win) == data.resolve()
    empty = tmp_path / "solo"
    empty.mkdir()
    assert P.follow_pointer(empty) == empty  # no pointer → folder itself


def test_jsh_home_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("JSH_HOME", str(tmp_path / "X"))
    assert P.app_home() == (tmp_path / "X").resolve()


def test_translocated_and_unwritable_homes_are_reported(tmp_path, monkeypatch):
    transloc = tmp_path / "AppTranslocation" / "ABC" / "d" / "JobSearchManager"
    with pytest.raises(P.PortableHomeError, match="App Translocation"):
        P.check_writable(transloc)

    def deny(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", deny)
    with pytest.raises(P.PortableHomeError, match="No se puede escribir"):
        P.check_writable(tmp_path / "ro")


def test_main_exits_cleanly_when_home_unusable(tmp_path, capsys):
    blocker = tmp_path / "soy_un_archivo"
    blocker.write_text("x")
    with pytest.raises(SystemExit) as exc:
        main(["--home", str(blocker / "sub"), "--no-browser"])
    assert exc.value.code == 2
    assert "no puede iniciar" in capsys.readouterr().out


# ---------------------------------------------------------------- packaging files
@pytest.mark.parametrize("rel", ["build_mac.sh", "packaging/dist_build_mac.sh",
                                 "packaging/Iniciar JobSearchManager.command"])
def test_unix_scripts_have_lf_and_shebang(rel):
    data = (ROOT / rel).read_bytes()
    assert b"\r\n" not in data, f"{rel} has CRLF line endings (breaks bash on macOS)"
    assert data.startswith(b"#!/"), f"{rel} needs a shebang"


@pytest.mark.parametrize("rel", ["build.bat", "packaging/dist_build.bat"])
def test_windows_batch_files_have_crlf(rel):
    data = (ROOT / rel).read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")


def _resolved(platform: str, machine: str) -> set[str]:
    env = {**default_environment(), "sys_platform": platform, "platform_machine": machine,
           "platform_system": {"darwin": "Darwin", "win32": "Windows"}[platform]}
    names = set()
    for line in (ROOT / "requirements-lock.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            req = Requirement(line)
            assert len(req.specifier) == 1 and next(iter(req.specifier)).operator == "==", line
            if req.marker is None or req.marker.evaluate(env):
                names.add(req.name.lower())
    return names


def test_lockfile_resolves_for_windows_and_macos():
    win, mac_arm, mac_x64 = _resolved("win32", "AMD64"), _resolved("darwin", "arm64"), _resolved("darwin", "x86_64")
    for core in ("nicegui", "sqlalchemy", "pydantic", "openpyxl", "httpx", "pyinstaller"):
        assert core in win and core in mac_arm and core in mac_x64
    assert {"pefile", "pywin32-ctypes"} <= win and not ({"pefile", "pywin32-ctypes"} & mac_arm)
    assert {"macholib", "uvloop"} <= mac_arm and {"macholib", "uvloop"} <= mac_x64
    assert not ({"macholib", "uvloop"} & win)


# ---------------------------------------------------------------- Mac Excel CSV
def test_mac_roman_csv_with_classic_mac_line_endings():
    text = "Puesto,Empresa,País\rIngeniería de Datos,Señal SA,México\r"
    data = text.encode("mac_roman")
    headers, rows, enc = read_csv_bytes(data, "mac_roman")
    assert enc == "mac_roman" and headers == ["Puesto", "Empresa", "País"]
    assert rows == [{"Puesto": "Ingeniería de Datos", "Empresa": "Señal SA", "País": "México"}]
    assert suggest_mapping(headers)["País"] == "job_country"
    # auto-detection cannot know it is Mac Roman: it decodes without crashing, user can switch encoding
    _, rows_auto, enc_auto = read_csv_bytes(data)
    assert enc_auto == "cp1252" and rows_auto[0]["Puesto"] != "Ingeniería de Datos"


def test_utf8_csv_with_bom_and_crlf():
    data = "\ufeffPuesto;Empresa\r\nAnalista;Ñandú\r\n".encode("utf-8")
    headers, rows, enc = read_csv_bytes(data)
    assert enc == "utf-8-sig" and headers == ["Puesto", "Empresa"] and rows[0]["Empresa"] == "Ñandú"
