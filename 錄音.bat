@echo off
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" goto novenv
start "KTV" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" -m ktvrec
exit /b 0
:novenv
echo 找不到虛擬環境，請先執行 setup.bat
pause
exit /b 1