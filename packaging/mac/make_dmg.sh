#!/bin/sh
# Packt build/dist/Moodle-Sync.app in ein DMG mit Verknüpfung zum Programme-Ordner.
set -e
cd "$(dirname "$0")/../.."
VER=$(python3 -c "import re;print(re.search(r'__version__ = \"([^\"]+)\"', open('moodle_sync.py', encoding='utf-8').read()).group(1))")
STAGE=build/dmg
rm -rf "$STAGE" && mkdir -p "$STAGE" build/installer
cp -R build/dist/Moodle-Sync.app "$STAGE/"
ln -s /Applications "$STAGE/Programme"
hdiutil create -volname "Moodle-Sync" -srcfolder "$STAGE" -ov -format UDZO "build/installer/Moodle-Sync-$VER.dmg" >/dev/null
echo "build/installer/Moodle-Sync-$VER.dmg"
