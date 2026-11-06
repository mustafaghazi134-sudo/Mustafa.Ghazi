@echo off
REM tradebot one-click Windows setup (PAPER TRADING ONLY).
REM Steps: check/install Git + Python (asks first) -> clone/update branch -> venv -> deps -> tests -> selftest -> preflight.
REM Download this file anywhere (e.g. your trading folder) and double-click it, or run it from inside the cloned repo.
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set REPO=https://github.com/mustafaghazi134-sudo/Mustafa.Ghazi
set BRANCH=claude/inspiring-edison-6xsm35

echo ============================================================
echo  tradebot Windows setup  (real-money execution stays OFF)
echo ============================================================

REM ---- 1. Git ---------------------------------------------------------------
where git >nul 2>nul
if errorlevel 1 (
  echo Git was not found on this PC.
  choice /M "Install Git for Windows now via winget (system change)"
  if errorlevel 2 (echo Skipped. Install Git from https://git-scm.com/download/win and re-run. & pause & exit /b 1)
  winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
  if errorlevel 1 (echo winget could not install Git. Install it manually and re-run. & pause & exit /b 1)
  echo Git installed. Close this window, open a NEW terminal, and run setup_windows.bat again so PATH refreshes.
  pause & exit /b 0
)
for /f "tokens=*" %%v in ('git --version') do echo Found %%v

REM ---- 2. Python 3.11+ ------------------------------------------------------
set PY=
py -3.13 -c "import sys" >nul 2>nul && set PY=py -3.13
if not defined PY py -3.12 -c "import sys" >nul 2>nul && set PY=py -3.12
if not defined PY py -3.11 -c "import sys" >nul 2>nul && set PY=py -3.11
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul && set PY=python
)
if not defined PY (
  echo Python 3.11 or newer was not found.
  choice /M "Install Python 3.12 now via winget (system change)"
  if errorlevel 2 (echo Skipped. Install Python 3.12 from https://www.python.org/downloads/ and tick Add to PATH, then re-run. & pause & exit /b 1)
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  if errorlevel 1 (echo winget could not install Python. Install it manually and re-run. & pause & exit /b 1)
  echo Python installed. Close this window, open a NEW terminal, and run setup_windows.bat again so PATH refreshes.
  pause & exit /b 0
)
for /f "tokens=*" %%v in ('%PY% --version') do echo Found %%v

REM ---- 3. Clone or update the repo -------------------------------------------
if exist "..\pyproject.toml" (
  REM running from inside <repo>\TRADEBOT\windows
  cd ..\..
  echo Using existing checkout: %CD%
  git fetch origin %BRANCH% && git checkout %BRANCH% && git pull --ff-only origin %BRANCH%
) else (
  if exist "Mustafa.Ghazi\TRADEBOT\pyproject.toml" (
    cd Mustafa.Ghazi
    echo Updating existing clone: %CD%
    git fetch origin %BRANCH% && git checkout %BRANCH% && git pull --ff-only origin %BRANCH%
  ) else (
    echo Cloning %REPO% (branch %BRANCH%) into %CD%\Mustafa.Ghazi
    git clone --branch %BRANCH% %REPO% Mustafa.Ghazi || (echo Clone failed. Check your internet connection. & pause & exit /b 1)
    cd Mustafa.Ghazi
  )
)
cd TRADEBOT

REM ---- 4. Virtual environment + dependencies ---------------------------------
if not exist .venv (%PY% -m venv .venv || (echo Could not create .venv & pause & exit /b 1))
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -e ".[dev]" || (echo Dependency install failed. See errors above. & pause & exit /b 1)

REM ---- 5. Tests + offline self-test -------------------------------------------
echo.
echo Running test suite...
python -m pytest -q || (echo TESTS FAILED. Do not run the bot until this is fixed. & pause & exit /b 1)
echo.
tradebot selftest || (echo SELF-TEST FAILED. & pause & exit /b 1)

REM ---- 6. Config + live-market connectivity -----------------------------------
if not exist config.toml tradebot init
echo.
echo Checking live market connectivity (Jupiter, Solana RPC, Binance, DexScreener, CoinGecko)...
tradebot preflight
echo.
echo ------------------------------------------------------------
echo Setup finished in %CD%
echo   Paper trading:   windows\run_paper.bat     (never sends real orders)
echo   Status/report:   windows\status.bat
echo   EMERGENCY STOP:  windows\kill_switch.bat
echo Optional: create .env with ANTHROPIC_API_KEY=... to enable Claude research and signal vetting.
echo ------------------------------------------------------------
pause
