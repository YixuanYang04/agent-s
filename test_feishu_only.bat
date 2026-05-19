@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo Feishu-only Agent-S3 test
echo ============================================================
echo No folder watching and no GPT classification.
echo Project is required in this mode: test_feishu_only.bat "C:\path\file.txt" --project "project name"
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" -Mode feishu %*
if errorlevel 1 (
    echo.
    echo [ERROR] Feishu-only test failed. Check the log above.
)

pause
