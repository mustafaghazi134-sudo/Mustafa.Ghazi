@echo off
REM Continuous PAPER trading with real market prices. Safe by construction:
REM TRADEBOT_FORCE_PAPER=1 overrides any mode setting, so this launcher can never send a real order.
cd /d "%~dp0\.."
if not exist .venv\Scripts\activate.bat (echo Run windows\setup_windows.bat first. & pause & exit /b 1)
call .venv\Scripts\activate.bat
if exist .env (for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if not "%%a"=="" set "%%a=%%b")
set TRADEBOT_FORCE_PAPER=1
set TRADEBOT_LIVE_TRADING=
set SOLANA_PRIVATE_KEY=
if not exist config.toml tradebot init
echo Starting tradebot in PAPER mode with live market data. Close this window or press Ctrl+C to stop.
echo Logs: data\tradebot.log   Reports: data\reports\   Emergency stop: windows\kill_switch.bat
:loop
tradebot run --paper
echo.
echo Bot exited (code %errorlevel%). Restarting in 30 seconds... (Ctrl+C to quit)
timeout /t 30 >nul
goto loop
