@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo File auto-archive + Feishu Agent-S3  [FAST MODE]
echo ============================================================
echo Reflection is OFF -- each step uses 1 LLM call instead of 2.
echo Uses project .venv. First run installs dependencies automatically.
echo Config: .env ^(WATCH_DIR, OPENAI/API settings, ARCHIVE_*^)
echo.

set AGENT_S_ENABLE_REFLECTION=0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" -Mode watch %*
if errorlevel 1 (
    echo.
    echo [ERROR] File archive watcher failed. Check the log above.
)
pause
