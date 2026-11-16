@echo off
REM Runs one 4-hour collection round. Task Scheduler starts this file.
cd /d "%~dp0"
".venv\Scripts\python.exe" run.py collect >> logs\task.log 2>&1
