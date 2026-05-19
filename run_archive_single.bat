@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Single file once: classify + optional Feishu (no folder watch)

echo ============================================================
echo Single file: classify + optional Feishu Agent
echo Double-click: pick file. Or: run_archive_single.bat "C:\path\file.pdf"
echo Uses project .venv. First run installs dependencies automatically.
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_start.ps1" -Mode single %*
if errorlevel 1 (
    echo.
    echo [ERROR] Single archive run failed. Check the log above.
)
echo.
pause
