@echo off
rem Build a single-file exe with PyInstaller
cd /d "%~dp0"
set "PYTHON_CMD=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_CMD=.venv\Scripts\python.exe"
"%PYTHON_CMD%" -m pip install pyinstaller
"%PYTHON_CMD%" -m PyInstaller --noconfirm --onefile --windowed --name GomokuCropLabeller main.py
echo.
echo Done. Output: dist\GomokuCropLabeller.exe
pause
