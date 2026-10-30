@echo off
REM ---------------------------------------------------------------------------
REM Build the portable Windows x64 distribution (PyInstaller onedir).
REM Requires (BUILD machine only): Python 3.11 x64 ("py -3.11") and internet for pip.
REM Override the interpreter with:  set PYTHON=C:\path\to\python.exe  (used by CI).
REM macOS: use build_mac.sh on a Mac (PyInstaller cannot cross-compile).
REM The resulting dist\JobSearchManager\ folder runs WITHOUT Python installed.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not defined PYTHON set "PYTHON=py -3.11"
if not exist .venv\Scripts\python.exe (
    %PYTHON% -m venv .venv || goto :error
)
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements-lock.txt || goto :error

echo Running tests...
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider || goto :error

echo Building executable... (work dir in %%TEMP%% to avoid OneDrive file locks)
.venv\Scripts\pyinstaller.exe --noconfirm --clean --distpath dist --workpath "%TEMP%\jsh_pyinstaller_work" packaging\JobSearchManager.spec || goto :error

set DIST=dist\JobSearchManager
for %%D in (data excel config resumes exports backups logs source) do if not exist "%DIST%\%%D" mkdir "%DIST%\%%D"
copy /Y JOB_SEARCH_HARNESS_MASTER.md "%DIST%\" >nul
copy /Y README.md "%DIST%\" >nul
copy /Y INSTRUCCIONES.md "%DIST%\" >nul
copy /Y AGENT_GUIDE.md "%DIST%\" >nul
copy /Y packaging\dist_build.bat "%DIST%\build.bat" >nul
REM Reproducible source snapshot (no venv, no user data). Top-level folders are excluded by FULL path so
REM package folders with the same name (e.g. jsh\excel) are kept.
robocopy . "%DIST%\source" /E /NFL /NDL /NJH /NJS /XD "%CD%\.venv" "%CD%\build" "%CD%\dist" "%CD%\data" "%CD%\excel" "%CD%\exports" "%CD%\backups" "%CD%\logs" "%CD%\resumes" "%CD%\config" "%CD%\.git" "%CD%\.nicegui" .pytest_cache __pycache__ >nul
if %ERRORLEVEL% GEQ 8 goto :error
if not exist "%DIST%\source\jsh\excel\csv_import.py" (echo source snapshot incomplete & goto :error)

REM Zip for copying to another PC (dist\JobSearchManager-Windows-x64.zip)
REM tar.exe ships with Windows 10 1803+ and writes zip much faster than Compress-Archive
if exist dist\JobSearchManager-Windows-x64.zip del /q dist\JobSearchManager-Windows-x64.zip
"%SystemRoot%\System32\tar.exe" -a -c -f dist\JobSearchManager-Windows-x64.zip -C dist JobSearchManager || goto :error

echo.
echo OK: %DIST%\JobSearchManager.exe
echo Zip para copiar a otra PC: dist\JobSearchManager-Windows-x64.zip
exit /b 0

:error
echo BUILD FAILED
exit /b 1
