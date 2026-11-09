@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo Home Library v2.7 - Windows Portable build
echo ============================================
where py >nul 2>&1
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)
%PY% --version || (echo Python 3 not found. Install Python 3.11-3.13 for build only. & pause & exit /b 1)
%PY% -m venv .buildvenv || (pause & exit /b 1)
call .buildvenv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
if errorlevel 1 (echo Dependency install failed & pause & exit /b 1)
python -m PyInstaller --noconfirm --clean --onedir --windowed --name HomeLibrary --add-data "index.html;." --add-data "assets;assets" --hidden-import tkinter --collect-submodules fitz --collect-submodules PIL --collect-submodules openpyxl --hidden-import markdown_converter --hidden-import ocr_language --hidden-import photo_book_converter --hidden-import reportlab --collect-submodules reportlab app.py
if errorlevel 1 (echo EXE build failed & pause & exit /b 1)
if not exist "dist\HomeLibrary\data" mkdir "dist\HomeLibrary\data"
if not exist "dist\HomeLibrary\downloads" mkdir "dist\HomeLibrary\downloads"
if not exist "dist\HomeLibrary\covers" mkdir "dist\HomeLibrary\covers"
if not exist "dist\HomeLibrary\previews" mkdir "dist\HomeLibrary\previews"
if not exist "dist\HomeLibrary\attachments" mkdir "dist\HomeLibrary\attachments"
echo.
echo SUCCESS: dist\HomeLibrary\HomeLibrary.exe
echo Copy the WHOLE dist\HomeLibrary folder to another PC.
pause
