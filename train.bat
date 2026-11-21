@echo off
setlocal
cd /d "%~dp0"
title AI-Trader - TRAINING (window shows heartbeat; safe to minimize)
set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo ============================================================
echo  AI-Trader training started: %date% %time%
echo  (progress prints per symbol + heartbeat every 30s)
echo ============================================================
"%PY%" -m app.main train %*
echo.
echo ============================================================
echo  Training ended: %date% %time%
echo ============================================================
pause
