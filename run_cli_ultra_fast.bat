@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo File auto-archive + Feishu Agent-S3  [ULTRA FAST MODE]
echo ============================================================
echo 1. Reflection is OFF -- each step uses 1 LLM call instead of 2.
echo 2. Generation Model is set to a fast/lightweight model (gpt-4o-mini).
echo 3. Grounding coords caching is active.
echo.

set AGENT_S_ENABLE_REFLECTION=0
set ARCHIVE_LLM_MODEL=gpt-4o-mini
set AGENT_S_MODEL=gpt-4o-mini
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" -Mode watch %*
if errorlevel 1 (
    echo.
    echo [ERROR] File archive watcher failed. Check the log above.
)
pause
