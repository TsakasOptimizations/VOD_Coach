@echo off
rem Builds dist\VODCoach.exe (one file, no Python needed to run it).
rem build_store.bat calls this with its own output options (a folder instead of one file).
cd /d "%~dp0"
set OUT=%~1
if "%OUT%"=="" set OUT=--onefile --distpath dist
pip install -q -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --name "VODCoach" %OUT% ^
  --icon "%cd%\assets\icon.ico" ^
  --add-data "%cd%\index.html;." --add-data "%cd%\assets;assets" ^
  --add-data "%cd%\valorant.json;." --add-data "%cd%\lol.json;." --add-data "%cd%\valorant_ranks.json;." ^
  --collect-all imageio_ffmpeg --collect-all webview --collect-data tzdata ^
  --workpath build --specpath build app.py
