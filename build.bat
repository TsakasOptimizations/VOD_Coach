@echo off
rem Builds dist\VODCoach.exe (one file, no Python needed to run it).
rem build_store.bat calls this with its own output options (a folder instead of one file).
cd /d "%~dp0"
set OUT=%~1
if "%OUT%"=="" set OUT=--onefile --distpath dist
rem Bug reports go to the Discord webhook in report_webhook.txt (git-ignored). Without the file, reporting is off.
set HOOK=
if exist "%cd%\report_webhook.txt" (set HOOK=--add-data "%cd%\report_webhook.txt;.") else (echo NOTE: no report_webhook.txt, bug reports will be off in this build)
pip install -q -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --name "VODCoach" %OUT% ^
  --icon "%cd%\assets\icon.ico" ^
  --add-data "%cd%\index.html;." --add-data "%cd%\assets;assets" ^
  --add-data "%cd%\valorant.json;." --add-data "%cd%\lol.json;." --add-data "%cd%\valorant_ranks.json;." %HOOK% ^
  --collect-all imageio_ffmpeg --collect-all webview --collect-data tzdata ^
  --workpath build --specpath build app.py
