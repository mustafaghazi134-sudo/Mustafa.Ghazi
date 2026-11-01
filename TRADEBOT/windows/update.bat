@echo off
REM Update an existing install: back up config/.env, pull, update deps, apply the validated profile to YOUR
REM config.toml (keys preserved), run tests + preflight + asset verification, then 3 paper cycles on real data.
REM Never prints or overwrites API keys. Real-money trading stays disabled (paper mode is forced).
cd /d "%~dp0\.."
if not exist .venv\Scripts\activate.bat (echo Run windows\setup_windows.bat first. & pause & exit /b 1)
set STAMP=%DATE:~-4%%DATE:~4,2%%DATE:~7,2%-%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%
set STAMP=%STAMP: =0%
if not exist backups mkdir backups
if exist config.toml copy /y config.toml "backups\config.toml.%STAMP%" >nul && echo backed up config.toml -> backups\config.toml.%STAMP%
if exist .env copy /y .env "backups\.env.%STAMP%" >nul && echo backed up .env -> backups\.env.%STAMP%
git fetch origin claude/inspiring-edison-6xsm35 && git checkout claude/inspiring-edison-6xsm35 && git pull --ff-only origin claude/inspiring-edison-6xsm35 || (echo git update failed & pause & exit /b 1)
call .venv\Scripts\activate.bat
pip install -q -e ".[dev]" || (echo dependency update failed & pause & exit /b 1)
if not exist config.toml tradebot init
tradebot apply-profile || (echo profile update failed & pause & exit /b 1)
if exist .env (for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if not "%%a"=="" set "%%a=%%b")
set TRADEBOT_FORCE_PAPER=1
set TRADEBOT_LIVE_TRADING=
set SOLANA_PRIVATE_KEY=
echo.
echo === tests ===
python -m pytest -q || (echo TESTS FAILED & pause & exit /b 1)
echo.
echo === preflight ===
tradebot preflight
echo.
echo === asset verification ===
tradebot verify-assets
echo.
echo === 3 paper cycles on real market data (prices, signals, fills are logged) ===
tradebot run --paper --cycles 3
echo.
echo === status / report ===
tradebot status
tradebot report --decisions 10
echo.
echo Done. Continuous paper trading: windows\run_paper.bat
pause
