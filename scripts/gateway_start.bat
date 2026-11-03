@echo off
REM ===========================================================================
REM PVIaN Gateway manual start entry point.
REM
REM Double-click or call from the command line / Agent to start the gateway.
REM It delegates to gateway_service.py start, which checks for an existing
REM instance and refuses to start a second one on the same port.
REM
REM The window stays open so Ctrl+C can stop the gateway cleanly. For detached
REM operation (Task Scheduler), use the scheduled task instead of this .bat.
REM ===========================================================================

title PVIaN Gateway
cd /d "%~dp0\.."

REM Use the project venv python if present, otherwise fall back to PATH.
if exist ".venv\Scripts\pythonw.exe" (
    set "PYTHON=.venv\Scripts\pythonw.exe"
) else (
    if exist ".venv\Scripts\python.exe" (
        set "PYTHON=.venv\Scripts\python.exe"
    ) else (
        set "PYTHON=python"
    )
)

echo [gateway_start.bat] starting PVIaN gateway via gateway_service.py ...
"%PYTHON%" scripts\gateway_service.py start
set RC=%ERRORLEVEL%
if %RC%==0 (
    echo [gateway_start.bat] gateway started successfully.
) else (
    echo [gateway_start.bat] gateway start returned code %RC%.
    pause
)
exit /b %RC%
