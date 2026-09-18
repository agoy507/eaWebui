@echo off
setlocal

cd /d "%~dp0"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
set "WEBUI_URL=http://127.0.0.1:8000"

if not exist "%PYTHON_EXE%" (
    echo.
    echo [ERROR] Python virtual environment belum ditemukan:
    echo %PYTHON_EXE%
    echo.
    echo Ikuti bagian instalasi pada README.md terlebih dahulu.
    pause
    exit /b 1
)

curl.exe --silent --fail --max-time 2 "%WEBUI_URL%/api/health" >nul 2>&1

if errorlevel 1 (
    echo Menjalankan RSI Martingale WebUI...
    start "RSI Martingale WebUI Server" "%PYTHON_EXE%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
    ping 127.0.0.1 -n 3 >nul
) else (
    echo WebUI sudah berjalan.
)

if /i "%~1"=="--no-browser" exit /b 0

start "" "%WEBUI_URL%"
exit /b 0
