@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

echo [INFO] Project root: %CD%

set "PYTHON_EXE="

if exist "%CD%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
) else if exist "%CD%\venv\Scripts\python.exe" (
    set "PYTHON_EXE=%CD%\venv\Scripts\python.exe"
) else if exist "%CD%\env\Scripts\python.exe" (
    set "PYTHON_EXE=%CD%\env\Scripts\python.exe"
) else (
    set "PYTHON_EXE=python"
)

echo [INFO] Using Python: %PYTHON_EXE%

echo [INFO] Checking port 8000...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo [INFO] Killing process on port 8000: PID %%P
    taskkill /PID %%P /F >nul 2>&1
)

for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "ESTABLISHED"') do (
    echo [INFO] Killing connected process on port 8000: PID %%P
    taskkill /PID %%P /F >nul 2>&1
)

timeout /t 1 /nobreak >nul

echo [INFO] Running Django checks...
call "%PYTHON_EXE%" manage.py check
if errorlevel 1 (
    echo [ERROR] Django check failed. Server not started.
    pause
    exit /b 1
)

echo [INFO] Starting Django server on http://127.0.0.1:8000/
echo [INFO] Press Ctrl+C to stop.
call "%PYTHON_EXE%" manage.py runserver 127.0.0.1:8000 --noreload

set "EXIT_CODE=%ERRORLEVEL%"
echo [INFO] Server exited with code %EXIT_CODE%
pause
exit /b %EXIT_CODE%
