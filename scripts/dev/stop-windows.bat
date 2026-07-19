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
    echo [ERROR] AutoOnCall 停止命令失败，请查看上面的错误信息。
    exit /b 1
)

echo.
echo [OK] AutoOnCall 进程停止完成。容器仅在传入 --containers 时停止。
