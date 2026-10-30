@echo off
REM Rebuild this portable distribution from the bundled source snapshot.
REM Running JobSearchManager.exe does NOT need this; building needs Python 3.11 x64.
cd /d "%~dp0source"
call build.bat
