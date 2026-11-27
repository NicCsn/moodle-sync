"""Plattform-Tests: laufen auf macOS und Windows (lokal und auf GitHub), ohne Moodle-Zugang.

Aufruf: python -I tests/test_platform.py
Auf Windows wird echt geprüft: Anmeldeinformationsverwaltung, Aufgabenplanung, Dateisperre,
Verknüpfungen (nur mit CI=true, weil sie im echten Startmenü landen).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["MOODLE_SYNC_HOME"] = tempfile.mkdtemp(prefix="ms-test-")
sys.path.insert(0, str(ROOT))
import moodle_sync as ms  # noqa: E402
import moodle_ui as ui  # noqa: E402

ms.ensure_data()

CI = os.environ.get("CI") == "true"


class Namen(unittest.TestCase):
    def test_safe_name(self):
        for src, want in {"Teil 1: A?": "Teil 1_ A_", "Folie.": "Folie", "CON": "_CON", "nul.txt": "_nul.txt",
                          "...": "unbenannt", "a/b\\c": "a_b_c"}.items():
            self.assertEqual(ms.safe_name(src), want)

    def test_dateien_anlegbar(self):
        base = Path(tempfile.mkdtemp())
        for n in ("Teil 1: Grundrechte?", "Folie. ", "CON", "aux.pdf", "Übung <3>|*", "x" * 300):
            p = ms.safe_join(base, n)
            p.write_text("x", encoding="utf-8")
            self.assertTrue(p.exists(), n)


class Konfig(unittest.TestCase):
    def test_roundtrip_mit_echten_pfaden(self):
        home = str(Path.home() / "Studium" / "Mathe 1 – Übung" / "Moodle")
        cfg = {"target": str(Path.home() / "Moodle"), "schedule": ["08:00", "18:00"],
               "sites": {"uni": {"title": "Uni", "url": "https://m.example", "mode": "token",
                                 "courses": {"1": home, "2": "~/X"}}}}
        text = ui.dump_config(cfg, ui.read_comments(""))
        self.assertEqual(ms.toml_loads(text), cfg)
        self.assertEqual(ms.toml_loads("﻿" + text.replace("\n", "\r\n")), cfg)
        self.assertTrue(ms.is_path_label(home))
        self.assertFalse(ms.is_path_label("Mathe 1"))
        self.assertEqual(ui.clean_path(home + os.sep), home)
        with self.assertRaises(ms.SyncError):
            ui.clean_path(str(Path(home).anchor))  # Laufwerk/Wurzel

    def test_save_config_schreibt_utf8(self):
        cfg = {"target": "~/Überall", "sites": {}}
        ui.save_config(cfg)
        self.assertEqual(ms.load_config(), cfg)


class Sperre(unittest.TestCase):
    def test_zweiter_prozess_wartet_nicht(self):
        code = ("import sys;sys.path.insert(0,sys.argv[1]);import moodle_sync as ms\n"
                "with ms.run_lock(wait=False) as got: print(got)")
        with ms.run_lock(wait=False) as got:
            self.assertTrue(got)
            r = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True,
                               env=os.environ, timeout=60)
            self.assertEqual(r.stdout.strip(), "False", r.stderr)
        r = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True,
                           env=os.environ, timeout=60)
        self.assertEqual(r.stdout.strip(), "True", r.stderr)


@unittest.skipIf(sys.platform == "darwin" and not CI, "Mac: echter Schlüsselbund – nur auf GitHub")
class Zugangsdaten(unittest.TestCase):
    def test_speichern_lesen_loeschen(self):
        acc = f"test-{os.getpid()}"
        secret = "abc123_Ümlaut-xyz"
        try:
            ms.keychain_set(acc, secret)
            self.assertEqual(ms.keychain_get(acc), secret)
            ms.keychain_set(acc, secret + "2")
            self.assertEqual(ms.keychain_get(acc), secret + "2")
        finally:
            ms.keychain_delete(acc)
        self.assertIsNone(ms.keychain_get(acc))
        with self.assertRaises(ms.SyncError):
            ms.keychain_set(acc, "")


@unittest.skipUnless(ms.IS_WIN, "nur Windows")
class Aufgabenplanung(unittest.TestCase):
    def test_an_status_aus(self):
        try:
            ms.job_enable()
            st = ms.job_status()
            self.assertTrue(st["installed"] and st["loaded"] and st["uses_auto"], st)
        finally:
            ms.job_disable()
        self.assertFalse(ms.job_status()["installed"])


class Oberflaeche(unittest.TestCase):
    def test_server_schluessel_und_ping(self):
        httpd, url = ui.start_server()
        try:
            port = httpd.server_address[1]
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with self.assertRaises(urllib.error.HTTPError) as e:
                op.open(f"http://127.0.0.1:{port}/api/state", timeout=10)
            self.assertEqual(e.exception.code, 403)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"Cookie": f"ms_ui={ui.TOKEN}"})
            st = json.load(op.open(req, timeout=30))
            self.assertEqual(st["sep"], os.sep)
            info = {}
            self.assertTrue(ui._running_ui(info))
            # fremde Webseite (Origin) darf nichts auslösen
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sync", data=b"{}", method="POST",
                                         headers={"Cookie": f"ms_ui={ui.TOKEN}", "Origin": "http://evil.example",
                                                  "Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                op.open(req, timeout=10)
            self.assertEqual(e.exception.code, 403)
        finally:
            ui.stop_server(httpd)

    def test_zweiter_server_nicht_auf_demselben_port(self):
        a, _ = ui.start_server()
        b, _ = ui.start_server()
        try:
            self.assertNotEqual(a.server_address[1], b.server_address[1])
        finally:
            ui.stop_server(a)
            ui.stop_server(b)

    def test_sync_im_prozess(self):
        ui.save_config({"target": "~/Moodle", "sites": {}})
        ui.RUN.start(None)
        for _ in range(100):
            if not ui.RUN.running:
                break
            time.sleep(0.1)
        self.assertEqual(ui.RUN.exit, 0, ui.RUN.lines)


@unittest.skipUnless(ms.IS_WIN and CI, "nur auf GitHub-Windows (legt echte Verknüpfungen an)")
class Installer(unittest.TestCase):
    def test_install_uninstall(self):
        ms.cmd_install(["--hier", "--kein-job"])
        L = ms._launchers()
        self.assertTrue(L["cmd"].exists())
        r = ms._ps("[Environment]::GetFolderPath('Programs')")
        self.assertTrue((Path(r.stdout.strip()) / "Moodle-Sync.lnk").exists())
        out = subprocess.run(["cmd", "/c", str(L["cmd"]), "hilfe"], capture_output=True, text=True,
                             env=os.environ, timeout=60)
        self.assertIn("Moodle", out.stdout + out.stderr)
        ms.cmd_uninstall([])
        self.assertFalse(L["cmd"].exists())
        self.assertFalse((Path(r.stdout.strip()) / "Moodle-Sync.lnk").exists())

    def test_mitteilung_stuerzt_nicht_ab(self):
        ms.notify("Test von Moodle-Sync")


if __name__ == "__main__":
    unittest.main(verbosity=2)
