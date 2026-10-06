@echo off
REM Cut 9:16 REVIEW DRAFTS for the approved shortlist (global ranks from TOP20.md). Nothing is uploaded.
REM Usage: make_previews.bat            (uses the approved ranks below)
REM        make_previews.bat 2,6,7      (different ranks)
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (echo Run setup_windows.bat first. & pause & exit /b 1)
call .venv\Scripts\activate.bat
set RANKS=%~1
if "%RANKS%"=="" set RANKS=1,3,4,5,8,9,10,13,18,19
python preview.py --campaign ben-affleck-street-we-grew-up-on --ranks %RANKS%
echo.
echo Previews: exports\ben-affleck-street-we-grew-up-on\REVIEW_PREVIEWS\
pause
