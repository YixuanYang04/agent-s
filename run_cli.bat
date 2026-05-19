@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo Agent-S3 Windows one-click launcher
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" -Mode run %*
if errorlevel 1 (
    echo.
    echo [ERROR] Agent-S3 failed to start. Check the log above.
)

pause
