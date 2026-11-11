@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"_runtime\python.exe" -X utf8 manual_feedback.py
set "result=%ERRORLEVEL%"
if not "%result%"=="0" (
  echo Press any key to continue.
  pause >nul
)
exit /b %result%
