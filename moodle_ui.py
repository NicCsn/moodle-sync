"""Browser-Oberfläche für moodle-sync: `moodle-sync ui`.

Läuft nur lokal (127.0.0.1). Schutz:
- zufälliger Schlüssel pro Start (Cookie, SameSite=Strict); ohne ihn gibt es nichts,
- Host- und Origin-Prüfung (gegen DNS-Rebinding und fremde Webseiten),
- Passwörter/Cookies nur im POST-Body; gespeichert wird nur Token/Cookie im Schlüsselbund.
"""

from __future__ import annotations

import collections
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import moodle_sync as ms

UI_HTML = ms.HERE / "ui.html"
TOKEN = secrets.token_urlsafe(24)
PORT = 0
_cfg_lock = threading.Lock()
APP_MODE = False  # True, wenn die Oberfläche im App-Fenster läuft
SHOW = None  # Rückruf der App: Fenster nach vorn holen (zweiter Start)
LAST_SEEN = time.time()  # letzte Anfrage der Seite; ohne offene Seite beendet sich der Server
IDLE_EXIT = 15 * 60
UI_FILE = ms.DATA / "ui.json"  # Port + Schlüssel der laufenden Oberfläche (nur für diesen Benutzer lesbar)


# ---------------------------------------------------------------- config.toml schreiben (Kommentare bleiben)

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str, quote: bool = False) -> str:
    return k if (_BARE.match(k) and not quote) else json.dumps(k, ensure_ascii=False)


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    raise ms.SyncError(f"Kann {type(v).__name__} nicht in TOML schreiben")


_LINE_KEY = re.compile(r'^\s*("(?:[^"\\]|\\.)*"|[A-Za-z0-9_-]+)\s*=')
_INLINE = re.compile(r'^(.*?=\s*(?:"(?:[^"\\]|\\.)*"|\[[^\]#]*\]|[^#"\[]*?))\s*(#.*)$')


def read_comments(text: str) -> dict:
    """Merkt sich Kommentare, Leerzeilen und die Originalzeilen, damit Nics Formatierung bleibt."""
    table, pending, gap = "", [], False
    head, lead, inline, raw, tail = {}, {}, {}, {}, {}

    def block():
        return ([""] if gap else []) + pending

    for line in text.splitlines():
        st = line.strip()
        if st.startswith("[") and not st.startswith("[["):
            table_new = st.strip("[]").strip()
            if pending or gap:
                head[table_new] = block()
            pending, gap, table = [], False, table_new
            continue
        if not st:
            if pending:
                pending.append("")
            else:
                gap = True
            continue
        if st.startswith("#"):
            pending.append(line)
            continue
        m = _LINE_KEY.match(line)
        if not m:
            continue
        k = m.group(1)
        k = json.loads(k) if k.startswith('"') else k
        if pending or gap:
            lead[(table, k)] = block()
        pending, gap = [], False
        raw[(table, k)] = line
        if im := _INLINE.match(line):
            inline[(table, k)] = im.group(2)
    if pending:
        tail[table] = pending
    return {"head": head, "lead": lead, "inline": inline, "raw": raw, "tail": tail}


def _same_line(raw: str | None, k: str, v) -> bool:
    if raw is None:
        return False
    try:
        return ms.toml_loads(raw).get(k, object()) == v
    except ms.TOMLDecodeError:
        return False


def dump_config(cfg: dict, comments: dict, new_inline: dict | None = None) -> str:
    new_inline = new_inline or {}
    out: list[str] = []

    def table(path: list[str], d: dict):
        tname = ".".join(path)
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
        if path and (scalars or tname in comments["head"] or not d):
            h = comments["head"].get(tname)
            if h is None:
                h = [""] if out else []
            out.extend(h)
            out.append("[" + ".".join(_key(p) for p in path) + "]")
        is_courses = path[-1:] == ["courses"]
        for k, v in scalars.items():
            out.extend(comments["lead"].get((tname, k), []))
            raw = comments["raw"].get((tname, k))
            if (tname, k) not in new_inline and _same_line(raw, k, v):
                out.append(raw)
                continue
            line = f"{_key(k, quote=is_courses)} = {_val(v)}"
            com = new_inline.get((tname, k)) or comments["inline"].get((tname, k))
            out.append(f"{line}  {com}" if com else line)
        out.extend(comments["tail"].get(tname, []))
        for k, v in d.items():
            if isinstance(v, dict):
                table(path + [k], v)

    table([], cfg)
    return "\n".join(out).strip("\n") + "\n"


def save_config(cfg: dict, new_inline: dict | None = None) -> None:
    old = ms.CONFIG.read_text(encoding="utf-8-sig") if ms.CONFIG.exists() else (
        "# moodle-sync – angelegt über die Oberfläche (moodle-sync ui)\n"
        "# Zeiten (schedule): zu diesen Uhrzeiten wird geladen; verpasste Termine holt der Job nach.\n")
    text = dump_config(cfg, read_comments(old), new_inline)
    if ms.toml_loads(text) != cfg:  # Sicherheitsnetz: nur schreiben, wenn exakt wieder einlesbar
        raise ms.SyncError("config.toml konnte nicht sauber geschrieben werden – nichts geändert.")
    if ms.CONFIG.exists():
        ms.CONFIG.with_name("config.toml.bak").write_text(old, encoding="utf-8")
    tmp = ms.CONFIG.with_name("config.toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(ms.CONFIG)


# ---------------------------------------------------------------- Sync im Hintergrund

class _Out:
    """Leitet print() des Sync-Threads in die Live-Ausgabe; alles andere geht weiter wie bisher."""

    def __init__(self, run: "SyncRun", fallback):
        self.run, self.fallback = run, fallback
        self.encoding, self.errors = "utf-8", "replace"

    def write(self, text: str) -> int:
        if threading.current_thread() is self.run.thread:
            self.run.feed(text)
        elif self.fallback is not None:
            self.fallback.write(text)
        return len(text)

    def flush(self) -> None:
        if self.fallback is not None:
            self.fallback.flush()

    def isatty(self) -> bool:
        return False


class SyncRun:
    """Sync im selben Prozess (geht auch in der fertigen App ohne eigenes Python)."""

    def __init__(self):
        self.thread: threading.Thread | None = None
        self.lines: collections.deque[str] = collections.deque(maxlen=400)
        self.site: str | None = None
        self.exit: int | None = None
        self.started = 0
        self._buf = ""
        self._log = None
        self._installed = False

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def feed(self, text: str) -> None:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.lines.append(line)
            if self._log:
                self._log.write(line + "\n")
                self._log.flush()

    def start(self, site: str | None) -> None:
        if self.running:
            raise ms.SyncError("Es läuft schon ein Sync.")
        if not self._installed:
            sys.stdout, sys.stderr = _Out(self, sys.stdout), _Out(self, sys.stderr)
            self._installed = True
        self.lines.clear()
        self.site, self.exit, self.started = site, None, int(time.time())
        self.thread = threading.Thread(target=self._work, args=(site,), daemon=True)
        self.thread.start()

    def _work(self, site: str | None) -> None:
        ms.LOG.parent.mkdir(parents=True, exist_ok=True)
        with ms.LOG.open("a", encoding="utf-8") as self._log:
            self._log.write(f"== {time.strftime('%d.%m.%Y %H:%M')} aus der Oberfläche: {site or 'alle'}\n")
            try:
                cfg = ms.load_config()
                code = ms.cmd_sync(cfg, [site] if site else [], interactive=False)
            except Exception as e:  # noqa: BLE001 – jeder Fehler soll in der Oberfläche landen
                print(f"FEHLER: {e}")
                code = 1
            if self._buf:
                self.feed("\n")
            self._log = None
        self.exit = code

    def info(self) -> dict:
        return {"running": self.running, "site": self.site, "exit": self.exit,
                "started": self.started, "lines": list(self.lines)}


RUN = SyncRun()


# ---------------------------------------------------------------- API

def tilde(p: str | Path) -> str:
    """Pfad unterhalb des Benutzerordners als ~/… (bzw. ~\\… unter Windows)."""
    p = Path(p)
    try:
        rel = p.relative_to(Path.home())
    except ValueError:
        return str(p)
    return "~" if str(rel) == "." else "~" + os.sep + str(rel)


def clean_path(p: str) -> str:
    """Eingegebenen Ordner prüfen: muss absolut sein oder mit ~ beginnen; Schrägstrich am Ende weg."""
    p = (p or "").strip()
    while len(p) > 1 and p[-1] in "/\\" and not re.fullmatch(r"[A-Za-z]:[\\/]", p):
        p = p[:-1]
    if not ms.is_path_label(p):
        beispiel = "C:\\Users\\…" if ms.IS_WIN else "/Users/…"
        raise ms.SyncError(f"Bitte einen vollständigen Ordner angeben (mit ~ oder {beispiel}): {p or '(leer)'}")
    full = Path(os.path.expanduser(p))
    if full.parent == full or full == Path.home():  # nie direkt in C:\, / oder den Benutzerordner laden
        raise ms.SyncError(f"Bitte einen eigenen Unterordner wählen, nicht {p}.")
    return p


def _group(dest: Path) -> tuple[Path, str]:
    """Kursordner für die Kachel: …/Kurs/Moodle und …/Kurs/Moodle AG gehören zu …/Kurs."""
    if dest.name == "Moodle":
        return dest.parent, "Vorlesung"
    if dest.name.startswith("Moodle "):
        return dest.parent, dest.name[7:]
    return dest, "Moodle"


def api_state() -> dict:
    cfg = ms.load_config(required=False)
    st = ms.load_state()
    target = Path(os.path.expanduser(cfg.get("target", "~/Moodle")))
    sites = {}
    try:
        notes = read_comments(ms.CONFIG.read_text(encoding="utf-8-sig"))["inline"]
    except OSError:
        notes = {}
    for name, s in cfg.get("sites", {}).items():
        site = ms.site_cfg(cfg, name)
        kind = "token" if site["mode"] == "token" else "cookie"
        sst = st.get("sites", {}).get(name, {})
        courses = []
        for cid, label in s.get("courses", {}).items():
            dest, short = ms.course_dest(site, label, target)
            cst = sst.get("courses", {}).get(str(cid), {})
            gdir, part = _group(dest)
            courses.append({"id": cid, "label": short, "path": tilde(dest), "raw": label,
                            "raw_is_path": ms.is_path_label(label), "group": tilde(gdir), "group_name": gdir.name,
                            "part": part, "leaf": dest.name,
                            "moodle_title": notes.get((f"sites.{name}.courses", str(cid)), "").lstrip("# ").strip(),
                            "exists": dest.exists(), **{k: cst.get(k) for k in ("new", "skipped", "errors", "time")}})
        sites[name] = {
            "url": site["url"], "mode": site["mode"], "auto": ms.site_auto(s),
            "title": s.get("title", name.upper()), "has_login": ms.keychain_get(f"{name}-{kind}") is not None,
            "sciebo_max_mb": s.get("sciebo_max_mb", 300), "notes_hosts": s.get("notes_hosts", []),
            "courses": courses,
            **{k: sst.get(k) for k in ("last_ok", "last_try", "error", "expired", "offline", "running")},
        }
    now = ms.datetime.now()
    schedule = cfg.get("schedule", ms.DEFAULT_SCHEDULE)
    return {
        "sites": sites, "schedule": schedule, "target": tilde(target),
        "skip_ext": cfg.get("skip_ext", sorted(ms.SKIP_EXT)),
        "next_slot": int(ms.next_slot(now, schedule).timestamp()),
        "job": ms.job_status(), "sync": RUN.info(), "has_config": ms.CONFIG.exists(),
        "platform": "win" if ms.IS_WIN else "mac" if ms.IS_MAC else "linux", "sep": os.sep,
        "version": ms.__version__, "app": APP_MODE,
    }


def _site_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", name):
        raise ms.SyncError("Kurzname: nur a–z, 0–9 und -, mit Buchstaben am Anfang (z. B. meineuni).")
    return name


def _get_site(cfg: dict, name: str) -> dict:
    if name not in cfg.get("sites", {}):
        raise ms.SyncError(f"Unbekannte Uni: {name}")
    return cfg["sites"][name]


def api_courses(name: str) -> dict:
    cfg = ms.load_config()
    site = ms.site_cfg(cfg, name)
    chosen = site.get("courses", {})
    target = Path(os.path.expanduser(cfg.get("target", "~/Moodle")))
    out = []
    for cid, title in ms.client(site).courses():
        label = chosen.get(str(cid))
        dest = ms.course_dest(site, label, target)[0] if label else None
        out.append({"id": str(cid), "title": title, "selected": label is not None,
                    "path": tilde(dest) if dest else "",
                    "suggest": tilde(target / name / ms.safe_name(title))})
    return {"courses": out}


def api_post(path: str, b: dict) -> dict:
    if path == "/api/probe":
        info = ms.probe_site(b.get("url", ""))
        host = urllib.parse.urlsplit(info["url"]).hostname or ""
        parts = [p for p in host.split(".") if p not in ("moodle", "www", "de", "com", "org", "edu", "net")]
        info["suggest"] = re.sub(r"[^a-z0-9-]", "", (parts[0] if parts else "uni").lower())[:20] or "uni"
        return info

    if path == "/api/site/save":
        name = _site_name(b.get("name"))
        info = ms.probe_site(b.get("url", ""))
        with _cfg_lock:
            cfg = ms.load_config(required=False)
            sites = cfg.setdefault("sites", {})
            s = sites.get(name, {})
            s.update({"url": info["url"], "mode": info["mode"]})
            if info.get("cookie_name"):
                s["cookie_name"] = info["cookie_name"]
            if b.get("title"):
                s["title"] = str(b["title"])[:40]
            s.setdefault("courses", {})
            # courses-Tabelle ans Ende, damit TOML gültig bleibt
            s["courses"] = s.pop("courses")
            sites[name] = s
            cfg.setdefault("target", "~/Moodle")
            cfg.setdefault("schedule", list(ms.DEFAULT_SCHEDULE))
            cfg.setdefault("skip_ext", sorted(ms.SKIP_EXT))
            save_config(_ordered(cfg))
        return {"name": name, "mode": info["mode"]}

    if path == "/api/site/remove":
        with _cfg_lock:
            cfg = ms.load_config()
            _get_site(cfg, b.get("name"))
            del cfg["sites"][b["name"]]
            save_config(cfg)
        return {}

    if path == "/api/site/auto":
        with _cfg_lock:
            cfg = ms.load_config()
            s = _get_site(cfg, b.get("name"))
            s["auto"] = bool(b.get("auto"))
            cfg["sites"][b["name"]] = _courses_last(s)
            save_config(cfg)
        return {}

    if path == "/api/login":
        cfg = ms.load_config()
        site = ms.site_cfg(cfg, b.get("name", ""))
        if site["mode"] == "token":
            if not b.get("username") or not b.get("password"):
                raise ms.SyncError("Benutzername und Passwort angeben.")
            ms.login_token(site, b["username"], b["password"])
        else:
            if not b.get("cookie"):
                raise ms.SyncError("Cookie einfügen.")
            ms.store_cookie(site, b["cookie"])
            ms.update_state(site["name"], expired=False, error=None)
        return {}

    if path == "/api/courses/save":
        name = b.get("name", "")
        with _cfg_lock:
            cfg = ms.load_config()
            s = _get_site(cfg, name)
            courses, inline = {}, {}
            for c in b.get("courses", []):
                cid, p = str(c.get("id", "")), (c.get("path") or "").strip()
                if not cid.isdigit() or not p:
                    continue
                courses[cid] = clean_path(p)
                if c.get("title"):
                    inline[(f"sites.{name}.courses", cid)] = "# " + str(c["title"]).replace("\n", " ")[:120]
            s["courses"] = courses
            save_config(cfg, inline)
        return {}

    if path == "/api/settings":
        with _cfg_lock:
            cfg = ms.load_config(required=False)
            if "schedule" in b:
                times = sorted({t for t in b["schedule"] if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t)})
                if not times:
                    raise ms.SyncError("Mindestens eine Uhrzeit angeben.")
                cfg["schedule"] = times
            if "target" in b:
                cfg["target"] = clean_path(str(b["target"]))
            if "skip_ext" in b:
                cfg["skip_ext"] = [e.strip().lstrip(".").lower() for e in b["skip_ext"] if e.strip()]
            save_config(_ordered(cfg))
        return {}

    if path in ("/api/course/add", "/api/course/update"):
        name = b.get("name", "")
        with _cfg_lock:
            cfg = ms.load_config()
            s = _get_site(cfg, name)
            inline = {}
            for c in b.get("courses", []):
                cid = str(c.get("id", ""))
                if not cid.isdigit():
                    raise ms.SyncError("Ungültige Kurs-ID.")
                s.setdefault("courses", {})[cid] = clean_path(c.get("path") or "")
                if c.get("title"):
                    inline[(f"sites.{name}.courses", cid)] = "# " + str(c["title"]).replace("\n", " ")[:120]
            cfg["sites"][name] = _courses_last(s)
            save_config(cfg, inline)
        return {}

    if path == "/api/course/remove":
        name, cid = b.get("name", ""), str(b.get("id", ""))
        with _cfg_lock:
            cfg = ms.load_config()
            s = _get_site(cfg, name)
            if cid not in s.get("courses", {}):
                raise ms.SyncError("Kurs ist nicht ausgewählt.")
            del s["courses"][cid]
            save_config(cfg)
        return {}

    if path == "/api/site/settings":
        name = b.get("name", "")
        with _cfg_lock:
            cfg = ms.load_config()
            s = _get_site(cfg, name)
            if "title" in b:
                s["title"] = str(b["title"]).strip()[:40] or name.upper()
            if "auto" in b:
                s["auto"] = bool(b["auto"])
            if "sciebo_max_mb" in b:
                s["sciebo_max_mb"] = max(0, int(b["sciebo_max_mb"]))
            if "notes_hosts" in b:
                hosts = [h.strip().lower() for h in b["notes_hosts"] if h.strip()]
                if any(not re.fullmatch(r"[a-z0-9.-]+", h) for h in hosts):
                    raise ms.SyncError("Notiz-Server: nur Hostnamen wie codi.example.de, ohne https://")
                s["notes_hosts"] = hosts
            cfg["sites"][name] = _courses_last(s)
            save_config(cfg)
        return {}

    if path == "/api/sync":
        if b.get("name"):
            ms.site_cfg(ms.load_config(), b["name"])
        RUN.start(b.get("name") or None)
        return {}

    if path == "/api/job":
        (ms.job_enable if b.get("on") else ms.job_disable)()
        return ms.job_status()

    if path == "/api/pick":
        start = os.path.expanduser(b.get("start") or "~")
        while start and not os.path.isdir(start):
            start = os.path.dirname(start)
        picked = ms.pick_folder(start or str(Path.home()))
        return {"path": tilde(picked) if picked else None}

    if path == "/api/open":
        p = Path(os.path.expanduser(b.get("path", ""))).resolve()
        allowed = _allowed_dirs()
        if not any(p == a or a in p.parents for a in allowed):
            raise ms.SyncError("Dieser Ordner gehört nicht zu moodle-sync.")
        while not p.exists() and p != p.parent:
            p = p.parent
        ms.open_target(str(p))
        return {}

    if path == "/api/open-site":
        site = ms.site_cfg(ms.load_config(), b.get("name", ""))
        ms.open_target(site["url"] + "/my/")
        return {}

    raise ms.SyncError("Unbekannte Aktion")


def _courses_last(s: dict) -> dict:
    c = s.pop("courses", {})
    s["courses"] = c
    return s


def _ordered(cfg: dict) -> dict:
    """Einfache Werte vor Tabellen (sonst landet z. B. schedule in der falschen Tabelle)."""
    flat = {k: v for k, v in cfg.items() if not isinstance(v, dict)}
    flat.update({k: v for k, v in cfg.items() if isinstance(v, dict)})
    if "sites" in flat:
        flat["sites"] = {n: _courses_last(dict(s)) for n, s in flat["sites"].items()}
    return flat


def _allowed_dirs() -> list[Path]:
    cfg = ms.load_config(required=False)
    target = Path(os.path.expanduser(cfg.get("target", "~/Moodle")))
    dirs = [target.resolve()]
    for name, s in cfg.get("sites", {}).items():
        site = ms.site_cfg(cfg, name)
        if s.get("dedupe_root"):
            dirs.append(Path(os.path.expanduser(s["dedupe_root"])).resolve())
        for label in s.get("courses", {}).values():
            d = ms.course_dest(site, label, target)[0].resolve()
            dirs.append(d)
            if d.name == "Moodle" or d.name.startswith("Moodle "):
                dirs.append(d.parent)  # Kursordner, den die Kachel zeigt
    return dirs


def log_tail(n: int = 300) -> str:
    try:
        with ms.LOG.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 60000))
            return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-n:])
    except OSError:
        return ""


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "moodle-sync"

    def log_message(self, *a):  # keine Anfragen ins Terminal (könnten Daten enthalten)
        pass

    def _host_ok(self) -> bool:
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _authed(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ms_ui" and hmac.compare_digest(v, TOKEN):
                return True
        return False

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/" and hmac.compare_digest(q.get("t", [""])[0], TOKEN):
            return self._send(303, b"", "text/plain", {
                "Location": "/", "Set-Cookie": f"ms_ui={TOKEN}; Path=/; HttpOnly; SameSite=Strict"})
        if not self._authed():
            return self._send(403, "Bitte im Terminal „moodle-sync ui“ starten – der Link öffnet sich dann von selbst."
                              .encode(), "text/plain; charset=utf-8")
        global LAST_SEEN
        LAST_SEEN = time.time()
        try:
            if u.path == "/api/ping":
                return self._json(200, {"ok": True, "app": APP_MODE})
            if u.path == "/api/show" and SHOW:
                SHOW()
                return self._json(200, {"ok": True})
            if u.path == "/":
                return self._send(200, UI_HTML.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._json(200, api_state())
            if u.path == "/api/sync":
                return self._json(200, RUN.info())
            if u.path == "/api/log":
                return self._json(200, {"log": log_tail()})
            if u.path == "/api/courses":
                return self._json(200, api_courses(q.get("name", [""])[0]))
        except ms.SessionExpired as e:
            return self._json(409, {"error": str(e), "expired": True})
        except (ms.SyncError, urllib.error.URLError, OSError) as e:
            return self._json(400, {"error": str(e)})
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._host_ok() or not self._authed():
            return self._send(403, b"forbidden", "text/plain")
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._send(403, b"forbidden", "text/plain")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, b"json only", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
            return self._json(200, api_post(urllib.parse.urlsplit(self.path).path, body))
        except ms.SessionExpired as e:
            return self._json(409, {"error": str(e), "expired": True})
        except (ms.SyncError, urllib.error.URLError, OSError, ValueError) as e:
            return self._json(400, {"error": str(e)})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Unter Windows erlaubt SO_REUSEADDR, einen belegten Port doppelt zu öffnen – dort exklusiv binden
    allow_reuse_address = not ms.IS_WIN

    def server_bind(self):
        if ms.IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _running_ui(info_out: dict | None = None) -> str | None:
    """Läuft die Oberfläche schon? Dann deren Adresse (statt eine zweite zu starten)."""
    try:
        info = json.loads(UI_FILE.read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping",
                                     headers={"Cookie": f"ms_ui={token}", "Host": f"127.0.0.1:{port}"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2) as r:
            res = json.load(r)
            if res.get("ok"):
                if info_out is not None:
                    info_out.update(res, port=port, token=token)
                return f"http://127.0.0.1:{port}/?t={token}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _write_ui_file() -> None:
    ms.DATA.mkdir(parents=True, exist_ok=True)
    fd = os.open(UI_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"port": PORT, "token": TOKEN, "pid": os.getpid(), "app": APP_MODE}, f)


def _idle_watch(httpd: Server) -> None:
    while True:
        time.sleep(30)
        if not RUN.running and time.time() - LAST_SEEN > IDLE_EXIT:
            print("Keine offene Seite mehr – Oberfläche beendet sich.", flush=True)
            httpd.shutdown()
            return


def start_server() -> tuple[Server, str]:
    """Startet den Server im Hintergrund-Thread. Gibt (server, Start-Adresse mit Schlüssel) zurück."""
    global PORT
    httpd = None
    for port in range(8765, 8785):
        try:
            httpd = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise ms.SyncError("Kein freier Port zwischen 8765 und 8784.")
    PORT = httpd.server_address[1]
    _write_ui_file()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{PORT}/?t={TOKEN}"


def stop_server(httpd: Server) -> None:
    httpd.shutdown()
    httpd.server_close()
    try:
        if json.loads(UI_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
            UI_FILE.unlink()
    except (OSError, ValueError):
        pass


def serve(open_browser: bool = True) -> int:
    global PORT
    url = _running_ui()
    if url:
        print(f"Die Oberfläche läuft schon: {url}", flush=True)
        if open_browser:
            ms.open_target(url)
        return 0
    httpd = None
    for port in range(8765, 8785):
        try:
            httpd = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print("Kein freier Port zwischen 8765 und 8784.", file=sys.stderr)
        return 1
    PORT = httpd.server_address[1]
    _write_ui_file()
    url = f"http://127.0.0.1:{PORT}/?t={TOKEN}"
    print(f"Moodle-Sync-Oberfläche läuft: {url}\nBeenden mit Ctrl+C (oder Seite schließen, "
          f"nach {IDLE_EXIT // 60} Minuten beendet sie sich).", flush=True)
    threading.Thread(target=_idle_watch, args=(httpd,), daemon=True).start()
    if open_browser:
        ms.open_target(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        httpd.server_close()
        try:
            if json.loads(UI_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
                UI_FILE.unlink()
        except (OSError, ValueError):
            pass
    return 0
