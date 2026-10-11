@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_UPDATE_WINDOWS.ps1" %*
if errorlevel 1 (
  echo [ERROR] Xem thong bao loi phia tren.
  pause
  exit /b 1
)
pause
