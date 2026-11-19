@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Mediul Python nu exista. Ruleaza intai: powershell -ExecutionPolicy Bypass -File setup.ps1
  pause
  exit /b 1
)
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" run.py %*
pause
