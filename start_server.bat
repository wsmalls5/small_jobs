@echo off
title Small Jobs - Server Launcher

echo.
echo  ==============================
echo   Small Jobs Server Launcher
echo  ==============================
echo.

:: --- Step 1: Kill anything already on port 5001 ---
echo  Checking port 5001 for an existing server...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 5001 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Write-Host '  Stopping old server (PID' $_.OwningProcess ')...'; Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
echo  Port cleared.

:: --- Step 2: Wait for port to fully release ---
timeout /t 2 /nobreak >nul

:: --- Step 3: Start the Flask server in a minimized window ---
echo  Starting Flask server...
start "Small Jobs Server" /MIN python "c:\Users\Dandy admin\Documents\Small Jobs\scripts\small_jobs.py"

:: --- Step 4: Wait for Flask to boot ---
timeout /t 3 /nobreak >nul

:: --- Step 5: Open the app in the default browser ---
echo  Opening app in browser...
start http://127.0.0.1:5001

echo.
echo  Done! The app should be open in your browser.
echo  Server is running in the background (taskbar: "Small Jobs Server").
echo.
echo  To restart: just run this file again.
echo  To stop:    close the "Small Jobs Server" taskbar window.
echo.
pause
