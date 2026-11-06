@echo off
REM EMERGENCY STOP: halts new entries and flattens open (paper) positions on the bot's next cycle.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
tradebot kill --reason "manual kill from kill_switch.bat"
echo To resume after review:  tradebot resume
pause
