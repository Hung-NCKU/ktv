@echo off
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" goto novenv
"%~dp0.venv\Scripts\python.exe" -m ktvrec.cli %*
pause
exit /b 0
:novenv
echo 找不到虛擬環境，請先執行 setup.bat
pause
exit /b 1