@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo [INFO] Project root: %CD%

for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (
    echo [ERROR] Could not detect the current git branch.
    pause
    exit /b 1
)

echo [INFO] Current branch: %CURRENT_BRANCH%

git status --short
if errorlevel 1 (
    echo [ERROR] Git status failed.
    pause
    exit /b 1
)

echo [INFO] Pushing branch %CURRENT_BRANCH% to origin...
git push -u origin %CURRENT_BRANCH%
if errorlevel 1 (
    echo [ERROR] Git push failed.
    pause
    exit /b 1
)

echo [INFO] Push completed successfully.
pause
exit /b 0
