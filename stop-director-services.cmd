@echo off
setlocal
pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-director-services.ps1" -Action Stop
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" (
  echo.
  echo Director service stop failed. Press any key to close.
  pause >nul
)
exit /b %exitCode%
