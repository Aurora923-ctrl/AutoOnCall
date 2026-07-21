@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0..\.."

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON=venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

"%PYTHON%" scripts\dev\pycharm_one_click_stop.py %*
if errorlevel 1 (
    echo.
    echo [ERROR] AutoOnCall stop failed. Check the error details above.
    exit /b 1
)

echo.
echo [OK] AutoOnCall process stop completed. Containers are stopped only with --containers.
