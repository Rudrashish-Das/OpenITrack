@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\openitrack-gui.exe" (
  echo First run: installing OpenITrack...
  python -m venv .venv || goto :error
  .venv\Scripts\python -m pip install -q -e . || goto :error
)
start "" ".venv\Scripts\openitrack-gui.exe" %*
exit /b
:error
echo Setup failed. Make sure Python 3.12+ is installed and on PATH.
pause
