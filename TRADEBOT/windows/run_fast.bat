@echo off
REM Fast engine, PAPER ONLY: scan -> detect -> cost -> risk -> buy -> manage -> sell -> record, every 5 s.
REM Own ledger (data\fast.db); shares the kill switch (data\KILL) with the 4h engine; never sends a real order.
cd /d "%~dp0\.."
if not exist .venv\Scripts\activate.bat (echo Run windows\setup_windows.bat first. & pause & exit /b 1)
call .venv\Scripts\activate.bat
if exist .env (for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if not "%%a"=="" set "%%a=%%b")
set TRADEBOT_FORCE_PAPER=1
set TRADEBOT_LIVE_TRADING=
set SOLANA_PRIVATE_KEY=
if not exist config.toml tradebot init
echo Starting the FAST engine in PAPER mode (real Jupiter/Binance data, simulated fills). Ctrl+C to stop.
echo Report any time from another window: tradebot fast-report --hours 24
:loop
tradebot fast %*
echo.
echo Fast engine exited (code %errorlevel%). Restarting in 30 seconds... (Ctrl+C to quit)
timeout /t 30 >nul
goto loop
