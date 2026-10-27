@echo off
echo Stopping Genesis Services on ports 8841 and 3000...

:: Kill processes on port 8841
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8841 ^| findstr LISTENING') do (
    taskkill /F /PID %%a 2>nul
)

:: Kill processes on port 3000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :3000 ^| findstr LISTENING') do (
    taskkill /F /PID %%a 2>nul
)

:: Kill live scanner python process
for /f "tokens=2" %%a in ('wmic process where "commandline like '%%live_scanner.py%%' and name like 'python%%'" get ProcessId 2^>nul ^| findstr [0-9]') do (
    taskkill /F /PID %%a 2>nul
)

echo Genesis services stopped.
