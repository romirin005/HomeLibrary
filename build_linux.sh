#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
if [[ "$(uname -s)" != Linux ]]; then echo "Сборка Linux выполняется только на Linux"; exit 1; fi
python3 -m venv .buildvenv
source .buildvenv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onedir --name HomeLibrary --add-data 'index.html:.' --add-data 'assets:assets' --collect-submodules fitz --collect-submodules PIL --collect-submodules openpyxl --hidden-import markdown_converter --hidden-import ocr_language --hidden-import photo_book_converter --hidden-import reportlab --collect-submodules reportlab app.py
mkdir -p dist/HomeLibrary/{data,downloads,covers,previews,attachments}
cp run_linux.sh dist/HomeLibrary/run_linux.sh
chmod +x dist/HomeLibrary/run_linux.sh
mkdir -p release
# Keep files in the distribution; one executable alone is not sufficient.
tar -czf release/HomeLibrary-v2.7-linux-$(uname -m)-portable.tar.gz -C dist HomeLibrary
printf '\nPortable готов: release/HomeLibrary-v2.7-linux-%s-portable.tar.gz\n' "$(uname -m)"
if [[ "${1:-}" != '--appimage' ]]; then exit 0; fi
ARCH="$(uname -m)"
if [[ "$ARCH" != x86_64 && "$ARCH" != aarch64 ]]; then echo "AppImage: неподдерживаемая архитектура $ARCH"; exit 2; fi
rm -rf AppDir
mkdir -p AppDir/usr/bin
cp -a dist/HomeLibrary AppDir/usr/bin/HomeLibrary
cp appimage/AppRun AppDir/AppRun
cp appimage/HomeLibrary.desktop AppDir/HomeLibrary.desktop
cp assets/beekeeper.png AppDir/homelibrary.png
chmod +x AppDir/AppRun
if ! command -v appimagetool >/dev/null 2>&1; then
 echo 'Не найден appimagetool. Скачайте официальный appimagetool для вашей архитектуры, сделайте исполняемым и добавьте в PATH.'
 echo 'Подготовленный каталог AppDir сохранён; Portable уже собран.'
 exit 3
fi
ARCH="$ARCH" appimagetool AppDir "release/HomeLibrary-v2.7-${ARCH}.AppImage"
echo 'AppImage готов в release/'
