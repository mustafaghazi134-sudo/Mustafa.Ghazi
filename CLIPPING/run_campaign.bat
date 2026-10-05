@echo off
REM Analyze every video in source\library\ben-affleck-street-we-grew-up-on\ and build the TOP 20 report.
REM Nothing is rendered or uploaded. Pass extra flags through, e.g.  run_campaign.bat --force-transcribe
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (echo Run setup_windows.bat first. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python batch.py --campaign ben-affleck-street-we-grew-up-on --top 20 %*
echo.
echo Report: candidates\ben-affleck-street-we-grew-up-on\TOP20.md
pause
