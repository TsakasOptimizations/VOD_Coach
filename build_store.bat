@echo off
rem Builds dist\VODCoach.msix for the Microsoft Store (the Store signs it on upload).
rem Before a new Store release: bump VERSION in app.py AND Version="x.y.0.0" in store\AppxManifest.xml.
cd /d "%~dp0"
pip install -q -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onedir --windowed --name "VODCoach" ^
  --icon "%cd%\assets\icon.ico" ^
  --add-data "%cd%\index.html;." --add-data "%cd%\assets;assets" ^
  --add-data "%cd%\valorant.json;." --add-data "%cd%\lol.json;." --add-data "%cd%\valorant_ranks.json;." ^
  --collect-all imageio_ffmpeg --collect-all webview --collect-data tzdata ^
  --distpath build\store_app --workpath build\store_work --specpath build app.py || exit /b 1

rem Stage the package layout: manifest + tile icons + the app folder
rmdir /s /q build\msix 2>nul
mkdir build\msix
copy /y store\AppxManifest.xml build\msix\ >nul
xcopy /e /i /q /y store\Assets build\msix\Assets >nul
xcopy /e /i /q /y build\store_app\VODCoach build\msix\VODCoach >nul

for /f "delims=" %%m in ('powershell -NoProfile -Command "(Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin' -Recurse -Filter makeappx.exe | Where-Object FullName -like '*x64*' | Select-Object -Last 1).FullName"') do set MAKEAPPX=%%m
if not defined MAKEAPPX echo makeappx.exe not found - install the Windows SDK & exit /b 1
if not exist dist mkdir dist
"%MAKEAPPX%" pack /o /d build\msix /p dist\VODCoach.msix
