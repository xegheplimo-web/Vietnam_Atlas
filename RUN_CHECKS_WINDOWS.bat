@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 -m unittest discover -s tests -v
) else (
  python -m unittest discover -s tests -v
)
pause
