@echo off
:: Launch the dashboard + bar-close watcher (portable: uses venv if present).
setlocal
cd /d "%~dp0"
set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m app.main test-llm
"%PY%" -m app.main serve
pause
