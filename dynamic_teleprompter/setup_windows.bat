@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Dynamic Teleprompter - First Time Setup
py -3 setup_windows.py
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Setup ended with error code %RESULT%.
pause
exit /b %RESULT%
