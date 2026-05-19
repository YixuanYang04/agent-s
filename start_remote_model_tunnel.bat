@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo Agent-S3 remote UI-TARS SSH tunnel
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" -Mode tunnel %*
if errorlevel 1 (
    echo.
    echo [ERROR] SSH tunnel failed. Check the log above.
)

pause
