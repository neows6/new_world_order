@echo off
REM ============================================================
REM  New World Order (NWO) - Stock Trading Program Launcher
REM ============================================================
title NWO - New World Order Trading Dashboard
color 0A
mode con: cols=110 lines=34

echo.
echo   ================================================================
echo      NEW WORLD ORDER  ^|  Trading Dashboard Launcher
echo   ================================================================
echo.
echo     Project folder : C:\Users\neo_w\new_world_order
echo     Server URL     : http://localhost:8765
echo     Browser opens  : automatically in ~4 seconds
echo     To stop server : press Ctrl+C in this window
echo   ================================================================
echo.

REM --- Change to the project directory ---
cd /d "C:\Users\neo_w\new_world_order"
if errorlevel 1 (
    echo [ERROR] Could not cd into C:\Users\neo_w\new_world_order
    echo Make sure the folder exists.
    echo.
    pause
    exit /b 1
)

REM --- Confirm python is available ---
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 'python' was not found on PATH.
    echo Install Python or add it to PATH, then try again.
    echo.
    pause
    exit /b 1
)

REM --- Kick off a background PowerShell job that waits, then opens the dashboard in your default browser ---
start "" /min powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://localhost:8765/'"

echo  Starting uvicorn server...
echo  ----------------------------------------------------------------
echo.

REM --- Run the server (this window shows live logs) ---
python -m uvicorn monitor.dashboard:app --host 0.0.0.0 --port 8765 --reload

echo.
echo  ----------------------------------------------------------------
echo  Server has stopped.
echo  ----------------------------------------------------------------
pause
