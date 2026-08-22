@echo off
echo =============================================
echo   Videasy Player - Starting...
echo =============================================
echo.
echo Server: http://localhost:8765/player.html
echo.

start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8765/player.html"

python "%~dp0serve.py"

echo.
echo Server stopped.
pause
