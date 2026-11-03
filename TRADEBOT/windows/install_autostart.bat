@echo off
REM Keep the PAPER engine running across reboots so forward evidence accumulates without gaps (blocker:
REM "persistent machine", docs/LIVE_READINESS_STATUS.md section 5). PAPER ONLY: it starts windows\run_paper.bat,
REM which forces paper mode and restarts the engine 30 s after any exit.
REM Each system change is asked first. Nothing here touches .env, wallets, activation files or the ledger.
REM Undo: schtasks /Delete /TN "TRADEBOT paper engine" /F
setlocal
cd /d "%~dp0"
set TASK=TRADEBOT paper engine
set RUNNER=%~dp0run_paper.bat
if not exist "%RUNNER%" (echo run_paper.bat not found next to this file. & pause & exit /b 1)

echo ============================================================
echo  Auto-start for the PAPER engine (never sends real orders)
echo ============================================================
echo This registers a Windows scheduled task that starts
echo   %RUNNER%
echo when you sign in to Windows.
choice /M "Register the scheduled task now (system change)"
if errorlevel 2 (echo Skipped. & goto power)
schtasks /Create /TN "%TASK%" /TR "cmd /c \"\"%RUNNER%\"\"" /SC ONLOGON /RL LIMITED /F
if errorlevel 1 (echo Could not register the task. & pause & exit /b 1)
echo Registered. It starts at your next sign-in. To start it now: schtasks /Run /TN "%TASK%"

:power
echo.
echo A sleeping PC runs nothing: open positions would have no exits (paper or live).
echo Current sleep timeout on mains power (0 = never):
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | findstr /C:"Current AC"
choice /M "Set sleep on mains power to Never (system change; screen can still turn off)"
if errorlevel 2 (echo Left unchanged. & goto done)
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
echo Sleep and hibernate on mains power set to Never.

:done
echo.
echo Also worth checking (not changed by this script):
echo  - Windows Update "active hours", so restarts happen when you can sign in again;
echo  - the PC signs in after a restart only when you sign in: the task starts at sign-in.
echo Status at any time: windows\status.bat     Emergency stop: windows\kill_switch.bat
pause
