# PyInstaller-Rezept für die Moodle-Sync-App (macOS .app bzw. Windows-Ordner mit Moodle-Sync.exe).
# Bauen: pyinstaller --noconfirm moodle-sync.spec   (vorher: pip install -r requirements-app.txt)
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH)
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "moodle_sync.py").read_text(encoding="utf-8")).group(1)
MAC, WIN = sys.platform == "darwin", sys.platform == "win32"

hidden = ["moodle_ui", "moodle_sync"]
if MAC:
    hidden += ["webview.platforms.cocoa"]
if WIN:
    hidden += ["webview.platforms.edgechromium", "webview.platforms.winforms", "clr", "pystray._win32"]

a = Analysis(
    [str(ROOT / "moodle_app.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "ui.html"), "."), (str(ROOT / "assets" / "tray-mac.png"), "assets"),
           (str(ROOT / "assets" / "tray-win.png"), "assets")],
    hiddenimports=hidden,
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Moodle-Sync",
    console=False,
    icon=str(ROOT / "assets" / ("icon.icns" if MAC else "icon.ico")),
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Moodle-Sync")
if MAC:
    app = BUNDLE(
        coll,
        name="Moodle-Sync.app",
        icon=str(ROOT / "assets" / "icon.icns"),
        bundle_identifier="de.local.moodle-sync.app",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "Moodle-Sync",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.education",
            "NSHumanReadableCopyright": "Moodle-Sync – freie Software, ohne Gewähr",
        },
    )
