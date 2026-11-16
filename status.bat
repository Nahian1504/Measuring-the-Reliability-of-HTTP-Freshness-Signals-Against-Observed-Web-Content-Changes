@echo off
REM Shows whether collection is healthy. Double-click it.
cd /d "%~dp0"
".venv\Scripts\python.exe" run.py status
pause
