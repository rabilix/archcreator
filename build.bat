@echo off
rem Builds the archcreator distributions into dist\ (run from anywhere).
rem   build.bat            exe folder + zip + wheel
rem   build.bat exe        exe folder + zip only
rem   build.bat wheel      wheel only
rem   build.bat test       run the tests first, then build everything
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo No .venv here. Create it first:  py -3 -m venv .venv  then  .venv\Scripts\pip install -e .[build]
    exit /b 2
)
set PY=.venv\Scripts\python.exe
set ARGS=

if /i "%1"=="exe"   set ARGS=--exe
if /i "%1"=="wheel" set ARGS=--wheel
if /i "%1"=="test" (
    "%PY%" -m pytest -q
    if errorlevel 1 exit /b 1
)

"%PY%" packaging\build.py %ARGS%
if errorlevel 1 exit /b 1

echo.
echo Built in %CD%\dist:
dir /b dist
endlocal
