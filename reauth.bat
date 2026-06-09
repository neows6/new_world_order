@echo off
REM ============================================================
REM  NWO - Schwab token re-authorization (one-step)
REM  Stops the running processes, runs the Schwab OAuth browser
REM  flow, then relaunches the dashboard + pipeline on the new token.
REM  The only manual step is logging in to Schwab in the browser.
REM ============================================================
title NWO - Schwab Re-Auth
cd /d "C:\Users\neo_w\new_world_order"

echo.
echo  [1/3] Stopping NWO processes (they hold the expired token in memory)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and ($_.CommandLine -match 'start_monitor' -or $_.CommandLine -match 'main\.py') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep 2"

echo.
echo  [2/3] Running Schwab OAuth flow...
echo        A URL will print - open it, log in to Schwab, then paste the
echo        full redirect URL back here when prompted.
echo  ----------------------------------------------------------------
python get_token.py
if errorlevel 1 (
    echo.
    echo  [ERROR] get_token.py failed - token NOT updated. Processes left stopped.
    echo  Fix the login and re-run reauth.bat.
    pause
    exit /b 1
)

echo.
echo  [3/3] Relaunching dashboard + pipeline on the new token...
powershell -NoProfile -Command "$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; Start-Process python -ArgumentList '-m','monitor.start_monitor' -WorkingDirectory 'C:\Users\neo_w\new_world_order' -WindowStyle Hidden -RedirectStandardOutput 'C:\Users\neo_w\new_world_order\logs\dashboard_start.log' -RedirectStandardError 'C:\Users\neo_w\new_world_order\logs\dashboard_start.err.log'"
powershell -NoProfile -Command "$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; Start-Process python -ArgumentList 'main.py' -WorkingDirectory 'C:\Users\neo_w\new_world_order' -WindowStyle Hidden -RedirectStandardOutput 'C:\Users\neo_w\new_world_order\logs\main_stdout.log' -RedirectStandardError 'C:\Users\neo_w\new_world_order\logs\main_stderr.log'"

echo.
echo  Done. Token refreshed and processes restarted.
echo  Dashboard: http://localhost:8765   (give it ~20s to come up)
echo  ----------------------------------------------------------------
pause
