@echo off
:: Live MT5 execution engine (portable: uses venv if present).
:: Default: engine loop per config.yaml -> execution.* (OFF + dry-run by default!)
:: Flags pass through, e.g.:  trade.bat --status | --once | --flatten | --loop 30
setlocal
cd /d "%~dp0"
set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m app.main trade %*
pause
