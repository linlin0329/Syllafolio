@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo Checking package integrity, imports and blank-window startup. No example data or development acceptance suite is run.
"_runtime\python.exe" -B -X utf8 check_prototype.py %*
set "result=%ERRORLEVEL%"
echo Read the actual report path printed above. Sound and workflow correctness require separate checks.
echo Press any key to continue.
pause >nul
exit /b %result%
