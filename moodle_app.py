"""Moodle-Sync als App: eigenes Fenster + Symbol in der Menüleiste (macOS) bzw. im Infobereich (Windows).

Ohne Argumente startet das Fenster. Mit Argumenten verhält sich die App wie der Befehl
`moodle-sync` (so ruft auch der Hintergrund-Job sie auf: `Moodle-Sync auto`).
Braucht pywebview (+ pystray und Pillow unter Windows); gebaut wird mit PyInstaller (moodle-sync.spec).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import moodle_sync as ms

ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"


def _status_text() -> str:
    import moodle_ui as ui
    if ui.RUN.running:
        return "Synchronisiert gerade …"
    st = ms.load_state().get("sites", {})
    oks = [s.get("last_ok") or 0 for s in st.values()]
    errs = [n for n, s in st.items() if s.get("error") and not s.get("offline")]
    if errs:
        return f"Problem bei {', '.join(errs)}"
    if any(oks):
        t = time.localtime(max(oks))
        today = time.localtime()
        when = time.strftime("%H:%M", t) if t[:3] == today[:3] else time.strftime("%d.%m. %H:%M", t)
        return f"Zuletzt synchronisiert: {when}"
    return "Noch nicht synchronisiert"


class App:
    def __init__(self):
        import webview
        import moodle_ui as ui
        self.webview, self.ui = webview, ui
        self.quitting = False
        self.window = None
        self.httpd = None
        self.tray = None

    # ---------------------------------------------------------------- Aktionen (aus Menü und Fenster)

    def show(self) -> None:
        if self.window:
            self.window.show()
            self.window.restore()
            if ms.IS_MAC:
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def sync_now(self) -> None:
        try:
            self.ui.RUN.start(None)
        except ms.SyncError:
            pass

    def quit(self) -> None:
        self.quitting = True
        if self.window:
            self.window.destroy()

    def _closing(self) -> bool:
        """Fenster schließen = ausblenden; das Symbol bleibt. Beenden über das Menü."""
        if self.quitting:
            return True
        self.window.hide()
        return False

    def _pick(self, start: str) -> str | None:
        res = self.window.create_file_dialog(self.webview.FileDialog.FOLDER, directory=start or str(Path.home()))
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else res

    # ---------------------------------------------------------------- Start

    def run(self) -> int:
        ui = self.ui
        info: dict = {}
        if ui._running_ui(info) and info.get("app"):  # läuft schon: nach vorn holen
            req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/show",
                                         headers={"Cookie": f"ms_ui={info['token']}"})
            try:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3).close()
            except OSError:
                pass
            return 0
        ui.APP_MODE = True
        ui.SHOW = self.show
        ms.PICK_HOOK = self._pick
        self.httpd, url = ui.start_server()
        try:
            ms.job_refresh()  # App verschoben oder aktualisiert: Job zeigt wieder auf diese App
        except ms.SyncError:
            pass
        self.window = self.webview.create_window(
            "Moodle-Sync", url, width=1240, height=820, min_size=(720, 520), text_select=True)
        self.window.events.closing += self._closing
        if ms.IS_MAC:
            self._mac_reopen()
        storage = ms.DATA / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        try:
            self.webview.start(self._after_start, private_mode=False, storage_path=str(storage))
        finally:
            if self.tray:
                self.tray.stop()
            ui.stop_server(self.httpd)
        return 0

    def _after_start(self) -> None:
        """Läuft im Hintergrund, sobald das Fenster steht."""
        try:
            self.tray = MacTray(self) if ms.IS_MAC else WinTray(self)
            self.tray.start()
        except Exception as e:  # noqa: BLE001 – ohne Symbol geht die App trotzdem
            print(f"Symbol nicht verfügbar: {e}")

    def _mac_reopen(self) -> None:
        """Klick aufs Dock-Symbol holt das ausgeblendete Fenster zurück."""
        from webview.platforms.cocoa import BrowserView
        app = self

        class MSAppDelegate(BrowserView.AppDelegate):
            def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):
                app.show()
                return True

        BrowserView.AppDelegate = MSAppDelegate


class WinTray:
    """Symbol im Windows-Infobereich (pystray)."""

    def __init__(self, app: App):
        import pystray
        from PIL import Image
        self.app = app
        M = pystray.MenuItem
        self.icon = pystray.Icon(
            "Moodle-Sync", Image.open(ASSETS / "tray-win.png"), "Moodle-Sync",
            menu=pystray.Menu(
                M("Moodle-Sync öffnen", lambda: app.show(), default=True),
                M(lambda _: _status_text(), None, enabled=False),
                M("Jetzt synchronisieren", lambda: app.sync_now(), enabled=lambda _: not app.ui.RUN.running),
                pystray.Menu.SEPARATOR,
                M("Beenden", lambda: app.quit()),
            ))

    def start(self) -> None:
        self.icon.run_detached()
        threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self) -> None:
        while True:
            time.sleep(20)
            try:
                self.icon.title = "Moodle-Sync – " + _status_text()
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                return

    def stop(self) -> None:
        self.icon.stop()


class MacTray:
    """Symbol in der macOS-Menüleiste (AppKit, läuft im Haupt-Thread von pywebview)."""

    def __init__(self, app: App):
        self.app = app
        self.item = None

    def start(self) -> None:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._build)

    def _build(self) -> None:
        import AppKit
        app = self.app

        class MSTrayTarget(AppKit.NSObject):
            def open_(self, sender):
                app.show()

            def sync_(self, sender):
                app.sync_now()

            def quit_(self, sender):
                app.quit()

            def menuNeedsUpdate_(self, menu):
                menu.itemAtIndex_(0).setTitle_(_status_text())
                menu.itemAtIndex_(2).setEnabled_(not app.ui.RUN.running)

        self.target = MSTrayTarget.alloc().init()
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(str(ASSETS / "tray-mac.png"))
        img.setSize_((18, 18))
        img.setTemplate_(True)  # macOS färbt das Symbol passend zur Menüleiste
        self.item.button().setImage_(img)
        self.item.button().setToolTip_("Moodle-Sync")
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self.target)

        def add(title, sel, enabled=True):
            mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
            if sel:
                mi.setTarget_(self.target)
            mi.setEnabled_(enabled)
            menu.addItem_(mi)

        add(_status_text(), None, False)
        add("Moodle-Sync öffnen", "open:")
        add("Jetzt synchronisieren", "sync:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        add("Moodle-Sync beenden", "quit:")
        self.item.setMenu_(menu)

    def stop(self) -> None:
        if self.item:
            import AppKit
            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self.item)


def main() -> int:
    if ms.FROZEN and ms.IS_MAC:  # gebautes Python kennt die Zertifikate des Macs nicht
        try:
            import certifi
            os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        except ImportError:
            pass
    args = sys.argv[1:]
    if args and not args[0].startswith("-psn"):  # Befehle (auch vom Hintergrund-Job)
        return ms.main([sys.argv[0], *args])
    ms._setup_output()
    ms.ensure_data()
    return App().run()


if __name__ == "__main__":
    sys.exit(main())
