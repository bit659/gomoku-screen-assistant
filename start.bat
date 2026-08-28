@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python Launcher was not found.
  echo Install 64-bit Python 3.11 from python.org and keep the "py" launcher enabled.
  if not defined CI pause
  exit /b 1
)

py -3.11 -c "import struct,sys; sys.exit(0 if sys.version_info[:2] == (3,11) and struct.calcsize('P') * 8 == 64 else 1)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 64-bit Python 3.11 is required.
  echo Check the installation with: py -3.11 --version
  if not defined CI pause
  exit /b 1
)

py -3.11 -c "import tkinter; tkinter.Tcl()" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.11 is missing Tcl/Tk.
  echo Re-run the Python installer and enable "tcl/tk and IDLE".
  if not defined CI pause
  exit /b 1
)

py -3.11 -c "import ctypes,sys; k=ctypes.windll.kernel32; sys.exit(0 if k.IsProcessorFeaturePresent(40) or k.IsProcessorFeaturePresent(37) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] This CPU does not provide AVX2 or SSE4.1, which Rapfi requires.
  if not defined CI pause
  exit /b 1
)

if not exist "%VENV_PYTHON%" (
  echo Creating the local Python environment...
  py -3.11 -m venv ".venv"
  if errorlevel 1 (
    echo [ERROR] Could not create .venv.
    if not defined CI pause
    exit /b 1
  )
)

"%VENV_PYTHON%" -c "import cv2,numpy,PIL,sys; sys.exit(0 if (numpy.__version__,cv2.__version__,PIL.__version__)==('2.4.6','4.13.0','12.2.0') else 1)" >nul 2>nul
if errorlevel 1 (
  echo Installing the locked Python dependencies...
  "%VENV_PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Check the network and try again.
    if not defined CI pause
    exit /b 1
  )
)

"%VENV_PYTHON%" app.py %*
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
  echo [ERROR] Gomoku Screen Assistant exited with code %APP_EXIT%.
  if not defined CI pause
)
exit /b %APP_EXIT%
