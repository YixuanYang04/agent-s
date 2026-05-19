@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo File auto-archive + optional Feishu Agent-S3
echo ============================================================
echo Uses project .venv. First run installs dependencies automatically.
echo Config: .env ^(WATCH_DIR, OPENAI/API settings, ARCHIVE_*^)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" -Mode watch %*
if errorlevel 1 (
    echo.
    echo [ERROR] File archive watcher failed. Check the log above.
)
pause
