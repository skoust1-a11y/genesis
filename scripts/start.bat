@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "ROOT_DIR=%SCRIPT_DIR%.."

echo Starting Genesis Services...

:: Start Backend in a new window
echo Starting Backend on port 8841...
cd /d "%ROOT_DIR%\backend"
start "Genesis Backend" cmd /k "uv run uvicorn app.main:app --host 0.0.0.0 --port 8841 --reload --reload-dir app"

:: Start Frontend in a new window
echo Starting Frontend on port 3000...
cd /d "%ROOT_DIR%\frontend"
start "Genesis Frontend" cmd /k "npm run dev"

:: Start Live Scanner in a new window
echo Starting Live Scanner...
cd /d "%ROOT_DIR%"
start "Genesis Live Scanner" cmd /k "backend\.venv\Scripts\python.exe scripts\live_scanner.py"

cd /d "%ROOT_DIR%"

echo.
echo Genesis services started.
echo Backend:  http://127.0.0.1:8841
echo Frontend: http://localhost:3000
echo Scanner:  Live Market Daemon
