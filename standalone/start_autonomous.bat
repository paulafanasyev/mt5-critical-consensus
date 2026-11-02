@echo off
chcp 65001 >nul
title Critical Consensus — autonomous demo
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python не найден / Python not found.
  start "" https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)
python scripts\run.py --mode autonomous %*
echo.
pause
