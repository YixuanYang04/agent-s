@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo Agent-S3 Windows endpoint test
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" -Mode test %*
if errorlevel 1 (
    echo.
    echo [ERROR] Test failed. Check the log above.
)

pause
