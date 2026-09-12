@echo off
setlocal
cd /d "%~dp0"
set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" scripts\check_env.py
pause
