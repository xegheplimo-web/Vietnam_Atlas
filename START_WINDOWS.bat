@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 -m server.app --host 127.0.0.1 --port 8765
) else (
  python -m server.app --host 127.0.0.1 --port 8765
)
pause
