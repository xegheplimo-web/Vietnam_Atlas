@echo off
cd /d "%~dp0"
docker compose up --build -d
if errorlevel 1 (echo Failed. Make sure Docker Desktop is running. & pause & exit /b 1)
start http://localhost:8765
pause
