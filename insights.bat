@echo off
setlocal
cd /d "%~dp0"
set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m app.main insights %*
pause
