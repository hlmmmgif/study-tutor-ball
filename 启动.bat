@echo off
rem ============================================================
rem  Launcher for tutor_ball.py  (ASCII only on purpose:
rem  cmd.exe parses .bat with the system ANSI codepage, so
rem  non-ASCII text here can break parsing on some machines.)
rem ============================================================
cd /d "%~dp0"

rem ---- 1) prefer the py launcher (handles multiple versions) ----
where py >nul 2>nul
if errorlevel 1 goto trypython
py -3 -c "import tkinter" >nul 2>nul
if errorlevel 1 goto trypython
set "PY=py -3"
goto found

rem ---- 2) fall back to python ----
:trypython
where python >nul 2>nul
if errorlevel 1 goto trypython3
python -c "import tkinter" >nul 2>nul
if errorlevel 1 goto trypython3
set "PY=python"
goto found

rem ---- 3) fall back to python3 ----
:trypython3
where python3 >nul 2>nul
if errorlevel 1 goto nopython
python3 -c "import tkinter" >nul 2>nul
if errorlevel 1 goto nopython
set "PY=python3"
goto found

:nopython
echo.
echo   [x] Python not found, or the Python found has no tkinter GUI library.
echo.
echo   Please install Python 3.9+ from https://www.python.org/downloads/
echo   During setup, make sure these two boxes are checked:
echo       [x] Add python.exe to PATH
echo       [x] tcl/tk and IDLE
echo.
echo   Then double-click this file again.
echo.
pause
exit /b 1

:found
rem ---- prefer pythonw (same folder as python) so no black console is left ----
if "%PY%"=="python3" set "PYW=pythonw3"
if "%PY%"=="python" set "PYW=pythonw"
if "%PY%"=="py -3" set "PYW=pyw -3"
where %PYW% >nul 2>nul
if errorlevel 1 goto console
start "" %PYW% "%~dp0tutor_ball.py"
exit /b 0

:console
echo Using console mode: %PY%
%PY% "%~dp0tutor_ball.py"
echo.
pause
exit /b 0
