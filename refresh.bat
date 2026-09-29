@echo off
REM ---------------------------------------------------------------------------
REM  Double-click this to refresh your Notion page.
REM
REM  It reads Google Calendar and Classroom, works out what matters today,
REM  and writes it to Notion. Takes about twenty seconds.
REM
REM  The window stays open so you can read what happened. Close it when done.
REM
REM  If Google asks you to sign in, a browser opens - that happens about once
REM  a week and takes a minute. See docs\SETUP.md.
REM ---------------------------------------------------------------------------

cd /d "%~dp0"

echo.
echo   Refreshing your day...
echo.

".venv\Scripts\python.exe" -m src.run_daily
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE%==0 (
    echo   Done. Your Notion page is up to date.
) else if %EXITCODE%==2 (
    echo   Something is not set up right - see the message above.
    echo   docs\SETUP.md explains each setting.
) else (
    echo   Part of it did not work - see the message above.
    echo   The rest still ran. Full details are in logs\agent.log
)

echo.
pause
