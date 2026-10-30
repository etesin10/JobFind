# -*- mode: python ; coding: utf-8 -*-
# PyInstaller ONEDIR build for the portable distribution (Windows x64, macOS arm64 / x86_64).
# PyInstaller cannot cross-compile: build on the target OS (build.bat on Windows, build_mac.sh on macOS,
# or the CI workflow in .github/workflows/build.yml).
#
# The output is a plain FOLDER (not a macOS .app bundle), so data/ excel/ backups/ live next to the
# executable on both systems and the whole folder can be copied between machines.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = collect_data_files("nicegui") + collect_data_files("docx")  # docx: default.docx template
hiddenimports = (collect_submodules("jsh") + collect_submodules("uvicorn")
                 + collect_submodules("mcp", filter=lambda n: not n.startswith(("mcp.cli", "mcp.__main__")))
                 + collect_submodules("mcp_types") + collect_submodules("docx")
                 + ["engineio.async_drivers.asgi", "sqlalchemy.dialects.sqlite"])

a = Analysis(
    [os.path.join(ROOT, "JobSearchManager.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JobSearchManager",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # console/Terminal window = visible log; closing it stops the local server
    # macOS: build for the architecture of the Python used to build (arm64 or x86_64).
    # universal2 would require every compiled dependency to ship universal2 wheels.
    target_arch=None,
    codesign_identity=None,  # PyInstaller ad-hoc signs on macOS (required on Apple Silicon)
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="JobSearchManager")
