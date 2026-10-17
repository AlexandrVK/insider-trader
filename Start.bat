@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title Запуск: %~n0

rem ===================================================
rem  НАСТРОЙКИ
rem ===================================================
rem Путь к python.exe. Пусто = системный Python (python / py -3 / python3 из PATH).
set "PYTHON_EXE=C:\python\python.exe"
rem Запускаемый файл. Пусто = имя этого bat-файла с расширением .py (например Start.bat = Start.py).
set "SCRIPT=app.py"
rem Виртуальное окружение: yes (по умолчанию) или no (ставить библиотеки в основную систему).
set "USE_VENV=yes"
set "VENV_DIR=.venv"
set "REQ_FILE=requirements.txt"
rem Цвета в логах: auto (по умолчанию), on или off. Читает само приложение (переменная COLORS).
set "COLORS=auto"
rem ===================================================

rem Логи Python в UTF-8 (вместе с chcp 65001 - без кракозябр)
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set "HINT="
set "CREATED="

echo ===================================================
echo   Подготовка окружения
echo ===================================================

rem ---- 1. Python ----
set "PY="
if not defined PYTHON_EXE goto find_system_python
if not exist "%PYTHON_EXE%" goto err_python_path
set PY="%PYTHON_EXE%"
goto python_found

:find_system_python
call :try_python python
if defined PY goto python_found
call :try_python py -3
if defined PY goto python_found
call :try_python python3
if defined PY goto python_found
goto err_no_python

:try_python
%* --version >nul 2>&1
if not errorlevel 1 set "PY=%*"
exit /b 0

:python_found
echo [ОК] Python: %PY%
%PY% --version

rem ---- 2. Файл скрипта ----
set "SCRIPT_NAME=%SCRIPT%"
if not defined SCRIPT_NAME set "SCRIPT_NAME=%~n0.py"
if not exist "%SCRIPT_NAME%" goto err_no_script
echo [ОК] Скрипт: %SCRIPT_NAME%

rem ---- 3. Окружение и зависимости ----
set "RUN=%PY%"
if not exist "%REQ_FILE%" goto no_requirements
if /i "%USE_VENV%"=="no" goto system_install

if exist "%VENV_DIR%\Scripts\python.exe" goto venv_ready
echo [ИНФО] Создаю виртуальное окружение %VENV_DIR% ...
set "CREATED=1"
%PY% -m venv "%VENV_DIR%"
if errorlevel 1 goto err_venv
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV_DIR%\Scripts\python.exe" -m pip install -r "%REQ_FILE%"
if errorlevel 1 goto err_pip
echo [ОК] Окружение создано, зависимости установлены.
goto venv_done

:venv_ready
echo [ОК] Виртуальное окружение найдено, проверяю зависимости...
"%VENV_DIR%\Scripts\python.exe" -m pip install -q -r "%REQ_FILE%"
if errorlevel 1 echo [ПРЕДУПРЕЖДЕНИЕ] Не удалось проверить зависимости, запускаю с тем, что установлено.

:venv_done
set RUN="%VENV_DIR%\Scripts\python.exe"
goto run

:system_install
echo [ИНФО] USE_VENV=no: устанавливаю библиотеки в основную систему...
%PY% -m pip install -r "%REQ_FILE%"
if errorlevel 1 goto err_pip_system
echo [ОК] Зависимости установлены.
goto run

:no_requirements
echo [ИНФО] Файл %REQ_FILE% не найден: виртуальное окружение не создаётся, зависимости не устанавливаются.

rem ---- 4. Запуск ----
:run
echo.
echo ===================================================
echo   Запуск: %SCRIPT_NAME%
echo ===================================================
echo.
%RUN% "%SCRIPT_NAME%"
set "CODE=%errorlevel%"
echo.
if not "%CODE%"=="0" echo [ИНФО] Скрипт завершился с кодом %CODE%.
pause
exit /b %CODE%

rem ===================================================
rem  КРИТИЧЕСКИЕ ОШИБКИ: красное окно, пауза, выход
rem ===================================================
:err_python_path
set "MSG=Python не найден по пути из настроек: %PYTHON_EXE%"
set "HINT=Исправьте PYTHON_EXE в начале этого файла или оставьте его пустым - тогда будет использован системный Python."
goto fail

:err_no_python
set "MSG=Python не найден в системе."
set "HINT=Установите Python с https://www.python.org/downloads/ (отметьте Add Python to PATH) или укажите путь к python.exe в PYTHON_EXE."
goto fail

:err_no_script
set "MSG=Файл скрипта не найден: %SCRIPT_NAME%"
set "HINT=Укажите имя файла в SCRIPT или положите %~n0.py рядом с этим bat-файлом."
goto fail

:err_venv
if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%" >nul 2>&1
set "MSG=Не удалось создать виртуальное окружение %VENV_DIR%."
set "HINT=Проверьте права на папку и что в установке Python есть модуль venv. Либо поставьте USE_VENV=no."
goto fail

:err_pip
if defined CREATED rmdir /s /q "%VENV_DIR%" >nul 2>&1
set "MSG=Не удалось установить зависимости из %REQ_FILE%."
set "HINT=Проверьте интернет и текст ошибки выше. Созданное окружение удалено: при следующем запуске оно будет создано заново."
goto fail

:err_pip_system
set "MSG=Не удалось установить зависимости из %REQ_FILE% в основную систему."
set "HINT=Проверьте интернет и текст ошибки выше, попробуйте запуск от имени администратора или поставьте USE_VENV=yes."
goto fail

:fail
color 4F
echo.
echo ===================================================
echo   [ОШИБКА] %MSG%
if defined HINT echo   %HINT%
echo ===================================================
echo.
echo Нажмите любую клавишу, чтобы закрыть окно (OK)...
pause >nul
exit /b 1
