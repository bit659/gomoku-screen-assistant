@echo off
rem Gomoku intersection crop labeller launcher
rem Uses the tool-local .venv (Python 3.11 with full tkinter) if present.
cd /d "%~dp0"
set "PYTHON_CMD=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_CMD=.venv\Scripts\python.exe"
"%PYTHON_CMD%" main.py
if errorlevel 1 pause
