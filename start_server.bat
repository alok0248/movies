@echo off
title Freebuff Desktop Server
echo ========================================
echo  Freebuff Desktop Server
echo ========================================
echo.

REM ---- Find Python ----
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if "%PY%"=="" python --version >nul 2>&1 && set "PY=python"
if "%PY%"=="" (
    echo ERROR: Python not found on this system.
    echo Install from https://www.python.org/downloads/ and check "Add to PATH".
    echo.
    pause
    exit /b 1
)
echo Using: %PY%
%PY% --version
echo.

REM ---- Check manage.py ----
if not exist "%~dp0manage.py" (
    echo ERROR: manage.py not found.
    echo Make sure this script is in the same folder as manage.py.
    echo Current folder: %~dp0
    echo.
    pause
    exit /b 1
)

REM ---- Move to script directory ----
cd /d "%~dp0"

REM ---- Stop existing server on port 8000 ----
echo Checking for existing server on port 8000...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr :8000 ^| findstr LISTENING') do (
    echo Found server PID: %%a - stopping it...
    taskkill /PID %%a /F >nul 2>&1
    timeout /t 2 /nobreak >nul
)
echo No existing server running.
echo.

REM ---- Activate venv if found ----
if exist "%~dp0venv\Scripts\activate.bat" (
    echo Activating virtual environment: venv
    call "%~dp0venv\Scripts\activate.bat"
) else if exist "%~dp0.venv\Scripts\activate.bat" (
    echo Activating virtual environment: .venv
    call "%~dp0.venv\Scripts\activate.bat"
) else if exist "%~dp0env\Scripts\activate.bat" (
    echo Activating virtual environment: env
    call "%~dp0env\Scripts\activate.bat"
) else (
    echo No virtual environment found - using system Python.
)
echo.

REM ---- Install dependencies ----
if exist "%~dp0requirements.txt" (
    echo Installing/updating dependencies...
    pip install -r "%~dp0requirements.txt" -q 2>&1
    echo.
)

REM ---- Run migrations ----
echo Applying database migrations...
%PY% manage.py migrate --run-syncdb 2>&1
echo.

REM ---- Create superuser if needed ----
%PY% -c "import django,os;os.environ['DJANGO_SETTINGS_MODULE']='movie_portal.settings';django.setup();from django.contrib.auth.models import User;exit(0 if User.objects.filter(is_superuser=True).exists() else 1)" >nul 2>&1
if errorlevel 1 (
    echo No admin user found. Creating one...
    set "DJANGO_SUPERUSER_USERNAME=admin"
    set "DJANGO_SUPERUSER_EMAIL=admin@example.com"
    set "DJANGO_SUPERUSER_PASSWORD=admin123"
    %PY% manage.py createsuperuser --noinput 2>&1
    echo   Created: admin / admin123
    echo.
)

REM ---- Start server ----
echo ========================================
echo  Server starting at http://127.0.0.1:8000
echo  Press Ctrl+C to stop
echo ========================================
echo.
%PY% manage.py runserver 0.0.0.0:8000
echo.
echo Server stopped.
pause
