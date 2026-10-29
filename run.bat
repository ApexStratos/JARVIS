@echo off
REM Run JARVIS Nexus in the foreground — Windows. For macOS/Linux, use run.sh.
cd /d "%~dp0"
where python3 >nul 2>nul
if %errorlevel%==0 (
    python3 server.py
) else (
    python server.py
)
