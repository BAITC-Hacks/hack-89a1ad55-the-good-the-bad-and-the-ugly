@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\run_demo.py %*
  goto finished
)
rem Reuse the prepared environment in the original delivered Codex workspace.
if exist "..\..\work\product-venv\Scripts\python.exe" (
  "..\..\work\product-venv\Scripts\python.exe" scripts\run_demo.py %*
  goto finished
)
py -3.12 -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1
if not errorlevel 1 (
  py -3.12 scripts\run_demo.py %*
  goto finished
)
python -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1
if not errorlevel 1 (
  python scripts\run_demo.py %*
  goto finished
)
rem Optional fallback for a desktop Codex host with no Python on PATH.
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\run_demo.py %*
  goto finished
)
echo Python 3.12 or newer was not found.
echo Install Python from python.org, enable Add Python to PATH, then try again.
echo Alternatively, follow the Docker instructions in README.md.
exit /b 2
:finished
set "demoExit=%errorlevel%"
if not "%demoExit%"=="0" (
  echo.
  echo Demo launch failed. The message above explains what to check.
  pause
)
exit /b %demoExit%
