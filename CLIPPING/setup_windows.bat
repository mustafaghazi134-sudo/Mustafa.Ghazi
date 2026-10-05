@echo off
REM One-time setup. Double-click or run from a terminal inside the CLIPPING folder.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install Python 3.10+ from https://www.python.org/downloads/ and tick "Add to PATH". & pause & exit /b 1)
where ffmpeg >nul 2>nul || (echo FFmpeg not found. Installing with winget... & winget install -e --id Gyan.FFmpeg & echo. & echo Close this window, open a NEW terminal, and run setup_windows.bat again. & pause & exit /b 1)
if not exist .venv (python -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
python tools\audit_env.py
echo.
echo Setup finished. Next: put the campaign videos in source\library\^<campaign^>\ and run run_campaign.bat
pause
