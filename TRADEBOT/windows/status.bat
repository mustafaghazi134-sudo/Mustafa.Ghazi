@echo off
REM Status, live-gate state, and the net-profitability report.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
tradebot status
echo.
tradebot report --days 7
pause
