@echo off
title NewMovies - Django Server
echo ==========================================
echo    NewMovies - Starting Django Server
echo ==========================================
echo.

REM Change to the script's directory
cd /d "%~dp0"

REM ── Check for Python ──────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found! Please install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

REM ── Activate virtual environment ──────────────
if exist "venv\Scripts\activate.bat" (
    echo [INFO] Activating virtual environment...
    call venv\Scripts\activate.bat
) else (
    echo [WARN] No venv found. Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
)

REM ── Install / update dependencies ─────────────
if exist "requirements.txt" (
    echo [INFO] Installing dependencies from requirements.txt...
    pip install -r requirements.txt --quiet
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies!
        pause
        exit /b 1
    )
)

REM ── Run migrations ────────────────────────────
echo [INFO] Running migrations...
python manage.py migrate --run-syncdb 2>nul

REM ── Start server ──────────────────────────────
echo.
echo [INFO] Starting server at http://127.0.0.1:8000
echo [INFO] Press Ctrl+C to stop the server.
echo.
python manage.py runserver 0.0.0.0:8000
pause
