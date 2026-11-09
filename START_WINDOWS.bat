@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo HomeLibrary v2.7 - Windows source launcher
if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 goto :failed
)
set "PY=.venv\Scripts\python.exe"
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    "%PY%" -m ensurepip --upgrade
    if errorlevel 1 goto :failed
)
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :failed
echo Starting HomeLibrary...
"%PY%" app.py
if errorlevel 1 goto :failed
exit /b 0
:failed
echo.
echo ERROR: Dependency installation or application startup failed.
echo Check internet connection and Python installation, then retry.
pause
exit /b 1
