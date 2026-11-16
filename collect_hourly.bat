@echo off
REM Runs one hourly round for the 100 hourly resources. Task Scheduler starts this file.
cd /d "%~dp0"
".venv\Scripts\python.exe" run.py collect --hourly >> logs\task.log 2>&1
