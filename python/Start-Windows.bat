@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 goto failure
)
".venv\Scripts\python.exe" -m pip install -r requirements-web.txt
if errorlevel 1 goto failure
".venv\Scripts\python.exe" run_aimt.py
if errorlevel 1 goto failure
exit /b 0
:failure
echo.
echo Khong chay duoc. Can Windows x64, Python 3.12 va ket noi Internet de cai thu vien.
pause
exit /b 1
