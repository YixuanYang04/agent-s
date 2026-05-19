@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Long-running folder watch: new files in WATCH_DIR (default: Desktop\wenjian guidang) -> classify -> optional Feishu Agent
REM Config: project .env -> WATCH_DIR, OPENAI_*, ARCHIVE_* 

echo ============================================================
echo Folder watch: classify + optional Feishu Agent
echo Folder: see WATCH_DIR in .env
echo Uses project .venv. First run installs dependencies automatically.
echo Ctrl+C to stop
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" -Mode watch %*
if errorlevel 1 (
    echo.
    echo [ERROR] File archive watcher failed. Check the log above.
)
echo.
pause
