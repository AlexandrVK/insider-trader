@echo off
chcp 1251 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

rem Укажите путь к Python 3.11+ (если пусто — будет искаться в PATH)
set "PYTHON_EXE=C:\python\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

"%PYTHON_EXE%" --version
if not exist ".venv\Scripts\python.exe" (
    echo [ИНФО] Создаю виртуальное окружение .venv ...
    "%PYTHON_EXE%" -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install --no-compile -r requirements.txt
)
echo [ИНФО] Запуск бота (веб-панель: http://127.0.0.1:8080) ...
.venv\Scripts\python.exe -m bot run
pause
