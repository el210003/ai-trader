@echo off
:: Fresh-machine bootstrap: create the venv, upgrade pip, install requirements,
:: and verify the environment. Idempotent - safe to re-run.
setlocal
cd /d "%~dp0"
echo === AI-Trader setup ===
echo.
echo [1/4] creating virtual environment (if missing)...
if not exist "venv\Scripts\python.exe" (
    python -m venv venv
    if errorlevel 1 ( echo   [!] failed to create venv - need Python 3.10+ on PATH & pause & exit /b 1 )
)
echo       venv ready.
echo.
echo [2/4] upgrading pip...
"venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
echo.
echo [3/4] installing requirements...
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 ( echo   [!] dependency install failed & pause & exit /b 1 )
echo.
echo [4/4] verifying...
"venv\Scripts\python.exe" -c "import app; from app.data.store import Store; import pandas, numpy, sklearn; print('       app imports OK; numpy', numpy.__version__, '| sklearn', sklearn.__version__)"
echo.
echo === Setup complete. Now run:  serve.bat   (or a config in config.yaml) ===
echo === Remember to set your LLM key, e.g.  setx MINIMAX_API_KEY yourkey   ===
pause
