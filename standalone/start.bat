@echo off
chcp 65001 >nul
title Critical Consensus — live demo
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python не найден / Python not found.
  echo  Открываю страницу установки / Opening the download page...
  echo  ^(!^) При установке отметь галочку "Add python.exe to PATH"
  start "" https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)
python scripts\run.py %*
echo.
pause
