@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Syllafolio with an empty workspace.
"_runtime\python.exe" -B -X utf8 run_launcher.py %*
set "result=%ERRORLEVEL%"
if not "%result%"=="0" (
  echo Startup was blocked or failed. Read the exact error and current log location above.
  echo Press any key to continue.
  pause >nul
)
exit /b %result%
