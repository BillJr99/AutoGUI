@echo off
:: start.bat — Launch AutoGUI with OSScreenObserver on Windows.
::
:: What it does:
::   1. Pulls all git submodules (OSScreenObserver, etc.)
::   2. Checks for required Python packages; prompts to install if missing.
::   3. Starts every submodule service in the background (port-checks first).
::   4. Launches AutoGUI (python main.py), forwarding any extra arguments.
::   5. After AutoGUI exits, kills any submodule service this script started.
::
:: Note: Ctrl+C during AutoGUI will prompt "Terminate batch job (Y/N)?".
::   Answering Y exits without cleanup; answering N lets cleanup run.
::   To avoid this, close the AutoGUI TUI with Ctrl+C inside the app.
::
:: Usage:
::   start.bat
::   start.bat "open a browser"
::   set PYTHON=py && start.bat    (use the py launcher instead of python)

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
:: strip trailing backslash
if "!SCRIPT_DIR:~-1!"=="\" set "SCRIPT_DIR=!SCRIPT_DIR:~0,-1!"
set "OSO_DIR=!SCRIPT_DIR!\OSScreenObserver"
set OSO_STARTED=0
set OSO_PID=

if not defined PYTHON set PYTHON=python

:: --- 1. Pull submodules -----------------------------------------------------
echo [start.bat] Updating submodules...
git -C "!SCRIPT_DIR!" submodule update --init --recursive
if !ERRORLEVEL! NEQ 0 (
    echo [start.bat] ERROR: git submodule update failed. Is git on PATH?
    exit /b 1
)

:: --- 2. Dependency check / install prompt -----------------------------------
!PYTHON! -c "import textual, flask" 2>nul
if !ERRORLEVEL! NEQ 0 (
    set /p "_INSTALL=[start.bat] Required packages appear missing. Install dependencies now? [Y/n] "
    if /i "!_INSTALL!"=="n" (
        echo [start.bat] Skipping install. Some features may not work.
    ) else (
        echo [start.bat] Running install-dependencies.cmd...
        call "!SCRIPT_DIR!\scripts\install-dependencies.cmd"
        if exist "!OSO_DIR!\requirements.txt" (
            echo [start.bat] Installing OSScreenObserver dependencies...
            !PYTHON! -m pip install --quiet -r "!OSO_DIR!\requirements.txt"
        )
    )
)

:: --- 3. Start OSScreenObserver if not already running ----------------------
curl -sf http://127.0.0.1:5001/api/healthz >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo [start.bat] OSScreenObserver is already running.
) else (
    echo [start.bat] Starting OSScreenObserver...

    :: Use PowerShell to start the process hidden and capture its PID via a
    :: temp file (avoids quoting issues with for /f on paths that have spaces).
    powershell -NoProfile -Command ^
        "$p = Start-Process -FilePath '!PYTHON!' ^
            -ArgumentList '!OSO_DIR!\main.py','--mode','inspect' ^
            -PassThru -WindowStyle Hidden; ^
        $p.Id | Out-File -Encoding ASCII '!TEMP!\oso_pid.txt'"
    if exist "!TEMP!\oso_pid.txt" (
        set /p OSO_PID=<"!TEMP!\oso_pid.txt"
        del "!TEMP!\oso_pid.txt" >nul 2>&1
    )
    set OSO_STARTED=1

    :: Wait up to 10 s for OSScreenObserver to become reachable.
    set WAITED=0
    :WAIT_OSO
    if !WAITED! GEQ 10 goto :OSO_READY
    curl -sf http://127.0.0.1:5001/api/healthz >nul 2>&1
    if !ERRORLEVEL! EQU 0 goto :OSO_READY
    timeout /t 1 /nobreak >nul
    set /a WAITED+=1
    goto :WAIT_OSO
    :OSO_READY

    echo [start.bat] OSScreenObserver started (PID !OSO_PID!).
)

:: --- 4. Launch AutoGUI ------------------------------------------------------
!PYTHON! "!SCRIPT_DIR!\main.py" %*

:: --- 5. Cleanup — kill OSScreenObserver if this script started it -----------
if !OSO_STARTED! EQU 1 (
    if defined OSO_PID (
        echo [start.bat] Stopping OSScreenObserver (PID !OSO_PID!)...
        taskkill /F /PID !OSO_PID! >nul 2>&1 || echo [start.bat] Could not stop OSScreenObserver.
    )
)

endlocal
