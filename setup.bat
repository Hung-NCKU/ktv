@echo off
cd /d "%~dp0"
echo === 建立虛擬環境 ===
py -3 -m venv "%~dp0.venv"
if not exist "%~dp0.venv\Scripts\python.exe" python -m venv "%~dp0.venv"
if not exist "%~dp0.venv\Scripts\python.exe" goto nopython
echo === 安裝套件 ===
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
echo.
echo === 檢查裝置 ===
"%~dp0.venv\Scripts\python.exe" -m ktvrec.cli --list
echo.
echo 完成，之後直接執行「錄音.bat」即可。
pause
exit /b 0
:nopython
echo 建立失敗：找不到 Python。請先安裝 Python 3.10 以上版本。
pause
exit /b 1