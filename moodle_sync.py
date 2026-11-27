#!/usr/bin/env python3
"""Lädt alle Dateien aus ausgewählten Moodle-Kursen herunter.

Zwei Modi:
  token  – über die Moodle-Mobile-API (Anmeldename + Kennwort). Stabil, vollautomatisch.
  cookie – über die Browser-Sitzung, wenn die Uni die App-Schnittstelle abgeschaltet hat.

Befehle:
  moodle-sync install        einrichten (Befehl, App-Starter, auf Wunsch Hintergrund-Job)
  moodle-sync uninstall      wieder entfernen (Einstellungen bleiben; --alles löscht auch sie)
  moodle-sync ui             Oberfläche im Browser (Unis, Kurse, Ordner, Status)
  moodle-sync                alles laden (alle Unis)
  moodle-sync <kurz>         nur eine Uni laden (Kurzname aus der Oberfläche)
  moodle-sync login <kurz>   Zugang neu einrichten (Token bzw. Cookie)
  moodle-sync kurse <kurz>   eingeschriebene Kurse mit IDs anzeigen
  moodle-sync job an|aus     Hintergrund-Job ein-/ausschalten (ohne Argument: Status)
  moodle-sync auto           was der Job aufruft: lädt nur, wenn ein Termin offen ist

Ist eine Browser-Sitzung abgelaufen, öffnet sich der Browser und das Skript
fragt direkt nach dem neuen Cookie. Alte Befehle (setup/courses/sync) gehen weiter.

Läuft auf macOS und Windows, nur mit der Python-Standardbibliothek (ab 3.9).
Zugangsdaten liegen im macOS-Schlüsselbund bzw. in der Windows-Anmeldeinformationsverwaltung.
"""

from __future__ import annotations

import errno
import getpass
import socket
import hashlib
import html
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
if IS_WIN:
    import msvcrt
else:
    import fcntl

try:
    import tomllib
    TOMLDecodeError = tomllib.TOMLDecodeError

    def toml_loads(s: str) -> dict:
        return tomllib.loads(s.lstrip("\ufeff"))  # Windows-Editoren speichern gern mit BOM
except ImportError:  # Python < 3.11 (z. B. das python3 von macOS): kleiner Parser für den Teil von TOML, den die Config nutzt
    tomllib = None

    class TOMLDecodeError(ValueError):
        pass

    _T_ESC = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}

    def _t_str(s: str, i: int) -> tuple[str, int]:
        q, out, i = s[i], [], i + 1
        while i < len(s):
            c = s[i]
            if c == q:
                return "".join(out), i + 1
            if c == "\\" and q == '"':
                n = s[i + 1:i + 2]
                if n in ("u", "U"):
                    w = 4 if n == "u" else 8
                    out.append(chr(int(s[i + 2:i + 2 + w], 16)))
                    i += 2 + w
                    continue
                if n not in _T_ESC:
                    raise TOMLDecodeError(f"Unbekanntes Escape \\{n}")
                out.append(_T_ESC[n])
                i += 2
                continue
            if c == "\n":
                raise TOMLDecodeError("Zeilenumbruch in Zeichenkette")
            out.append(c)
            i += 1
        raise TOMLDecodeError("Zeichenkette nicht geschlossen")

    def _t_ws(s: str, i: int, nl: bool = False) -> int:
        while i < len(s):
            if s[i] in " \t" or (nl and s[i] in "\r\n"):
                i += 1
            elif s[i] == "#" and nl:
                while i < len(s) and s[i] != "\n":
                    i += 1
            else:
                break
        return i

    def _t_val(s: str, i: int):
        c = s[i:i + 1]
        if c in ('"', "'"):
            return _t_str(s, i)
        if c == "[":
            arr, i = [], _t_ws(s, i + 1, True)
            while s[i:i + 1] != "]":
                v, i = _t_val(s, i)
                arr.append(v)
                i = _t_ws(s, i, True)
                if s[i:i + 1] == ",":
                    i = _t_ws(s, i + 1, True)
                elif s[i:i + 1] != "]":
                    raise TOMLDecodeError("Liste: , oder ] erwartet")
            return arr, i + 1
        m = re.compile(r"true|false|[+-]?[0-9][0-9_]*(\.[0-9_]+)?([eE][+-]?[0-9]+)?").match(s, i)
        if not m:
            raise TOMLDecodeError(f"Unbekannter Wert bei: {s[i:i + 20]!r}")
        t = m.group(0)
        if t in ("true", "false"):
            return t == "true", m.end()
        t = t.replace("_", "")
        return (float(t) if m.group(1) or m.group(2) else int(t)), m.end()

    def _t_key(s: str, i: int) -> tuple[list, int]:
        parts = []
        while True:
            i = _t_ws(s, i)
            if s[i:i + 1] in ('"', "'"):
                k, i = _t_str(s, i)
            else:
                m = re.compile(r"[A-Za-z0-9_-]+").match(s, i)
                if not m:
                    raise TOMLDecodeError(f"Schlüssel erwartet bei: {s[i:i + 20]!r}")
                k, i = m.group(0), m.end()
            parts.append(k)
            i = _t_ws(s, i)
            if s[i:i + 1] != ".":
                return parts, i
            i += 1

    def toml_loads(s: str) -> dict:
        s = s.lstrip("\ufeff")  # Windows-Editoren speichern gern mit BOM
        root, cur, i = {}, None, 0
        cur = root
        while True:
            i = _t_ws(s, i, True)
            if i >= len(s):
                return root
            if s[i] == "[":
                if s[i + 1:i + 2] == "[":
                    raise TOMLDecodeError("[[Tabellen-Listen]] werden nicht unterstützt")
                keys, i = _t_key(s, i + 1)
                if s[i:i + 1] != "]":
                    raise TOMLDecodeError("] erwartet")
                i += 1
                cur = root
                for k in keys:
                    cur = cur.setdefault(k, {})
                    if not isinstance(cur, dict):
                        raise TOMLDecodeError(f"{k} ist keine Tabelle")
            else:
                keys, i = _t_key(s, i)
                if s[i:i + 1] != "=":
                    raise TOMLDecodeError("= erwartet")
                v, i = _t_val(s, _t_ws(s, i + 1))
                t = cur
                for k in keys[:-1]:
                    t = t.setdefault(k, {})
                if keys[-1] in t:
                    raise TOMLDecodeError(f"{keys[-1]} doppelt")
                t[keys[-1]] = v
            i = _t_ws(s, i, True) if s[_t_ws(s, i):_t_ws(s, i) + 1] in ("#", "\n", "\r", "") else None
            if i is None:
                raise TOMLDecodeError("Zeilenende erwartet")

__version__ = "1.1.0"
FROZEN = bool(getattr(sys, "frozen", False))  # als fertige App (PyInstaller) gebaut
HERE = Path(__file__).resolve().parent  # Programmdateien
# Einstellungen und Stand liegen getrennt vom Programm, damit es sich ersetzen lässt
def _default_data() -> Path:
    if IS_WIN:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "moodle-sync"
    if IS_MAC:
        return Path.home() / "Library/Application Support/moodle-sync"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "moodle-sync"


DATA = Path(os.environ.get("MOODLE_SYNC_HOME") or _default_data())
CONFIG = DATA / "config.toml"
STATE = DATA / "state.json"   # letzter Lauf je Uni/Kurs (für auto und die Oberfläche)
LOCK = DATA / ".lock"
LOG = Path.home() / "Library/Logs/moodle-sync.log" if IS_MAC else DATA / "moodle-sync.log"
WIN_TASK = "moodle-sync"  # Name in der Windows-Aufgabenplanung
JOB_LABEL = "de.local.moodle-sync"
JOB_PLIST = Path.home() / f"Library/LaunchAgents/{JOB_LABEL}.plist"
DEFAULT_SCHEDULE = ["08:00", "18:00"]
KEYCHAIN_SERVICE = "moodle-sync"
UA = "moodle-sync/1.0 (personal course backup)"
DELAY = 0.3  # Sekunden zwischen Anfragen
# Dateiendungen, die nie geladen werden (überschreibbar mit skip_ext in config.toml)
SKIP_EXT = {"mp4", "mkv", "mov", "avi", "webm", "m4v", "wmv"}


def skip_file(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in SKIP_EXT if "." in name else False


class SyncError(Exception):
    pass


class SessionExpired(SyncError):
    pass


# ---------------------------------------------------------------- helpers

def load_config(required: bool = True) -> dict:
    if not CONFIG.exists():
        if not required:
            return {"target": "~/Moodle", "sites": {}}
        raise SyncError(f"{CONFIG} fehlt – moodle-sync ui starten oder config.example.toml kopieren.")
    try:
        return toml_loads(CONFIG.read_text(encoding="utf-8-sig"))
    except TOMLDecodeError as e:
        raise SyncError(f"{CONFIG} ist fehlerhaft: {e}") from None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def update_state(name: str, **fields) -> None:
    st = load_state()
    st.setdefault("sites", {}).setdefault(name, {}).update(fields)
    save_state(st)


@contextmanager
def run_lock(wait: bool):
    """Nur ein Sync gleichzeitig (Job, Terminal und Oberfläche)."""
    with LOCK.open("a") as f:
        try:
            if IS_WIN:
                f.seek(0)
                while True:  # LK_LOCK gibt nach 10 s auf, darum selbst warten
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if not wait:
                            raise BlockingIOError from None
                        time.sleep(1)
            else:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            if IS_WIN:
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


def site_auto(site: dict) -> bool:
    """Läuft die Uni automatisch? Standard: Token ja, Cookie nein (Session läuft ab)."""
    return bool(site.get("auto", site.get("mode") == "token"))


def last_slot(now: datetime, schedule: list[str]) -> datetime:
    """Letzter geplanter Zeitpunkt <= now (heute oder gestern)."""
    times = [datetime.strptime(t, "%H:%M").time() for t in schedule] or [datetime.min.time()]
    cands = [datetime.combine(now.date() - timedelta(days=d), t) for d in (0, 1) for t in times]
    return max(c for c in cands if c <= now)


def next_slot(now: datetime, schedule: list[str]) -> datetime:
    times = [datetime.strptime(t, "%H:%M").time() for t in schedule] or [datetime.min.time()]
    cands = [datetime.combine(now.date() + timedelta(days=d), t) for d in (0, 1) for t in times]
    return min(c for c in cands if c > now)


_OFFLINE_ERRNO = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ECONNREFUSED,
                  errno.ETIMEDOUT, errno.EADDRNOTAVAIL,
                  10050, 10051, 10060, 10061, 10065, 11001, 11002}  # Windows (WSAENETDOWN …)


def is_offline(e: BaseException) -> bool:
    """Netz weg (DNS, keine Route, Timeout) statt echter Fehler?"""
    if isinstance(e, urllib.error.HTTPError):
        return False
    if isinstance(e, urllib.error.URLError):
        e = e.reason if isinstance(e.reason, BaseException) else e
    return isinstance(e, (socket.gaierror, TimeoutError, socket.timeout)) or (
        isinstance(e, OSError) and e.errno in _OFFLINE_ERRNO)


def online(url: str, tries: int = 4, wait: float = 15) -> bool:
    """Ist die Moodle-Seite erreichbar? Nach dem Aufwachen braucht das WLAN oft etwas."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url + "/login/index.php", method="HEAD", headers={"User-Agent": UA})
            urllib.request.urlopen(req, timeout=10).close()
            return True
        except urllib.error.HTTPError:
            return True  # Server antwortet – Netz ist da
        except (urllib.error.URLError, OSError):
            if i + 1 < tries:
                time.sleep(wait)
    return False


def site_cfg(cfg: dict, name: str) -> dict:
    try:
        s = cfg["sites"][name]
    except KeyError:
        raise SyncError(f"Site '{name}' steht nicht in config.toml.") from None
    s = dict(s)
    s["name"] = name
    s["url"] = s["url"].rstrip("/")
    return s


# Geheimnisse: macOS-Schlüsselbund, Windows-Anmeldeinformationsverwaltung, sonst Datei mit Rechten 600

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    class _CRED(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CRED))]
    _advapi.CredReadW.restype = wintypes.BOOL
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_CRED), wintypes.DWORD]
    _advapi.CredWriteW.restype = wintypes.BOOL
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredDeleteW.restype = wintypes.BOOL
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
    _advapi.CredFree.restype = None
    _CRED_TYPE_GENERIC, _CRED_PERSIST_LOCAL_MACHINE = 1, 2


def _secret_file() -> Path:
    return DATA / ".secrets.json"


def keychain_get(account: str) -> str | None:
    if IS_MAC:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-g"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return None
        # -g schreibt `password: "wert"` – oder bei Umlauten `password: 0x<hex>  "…"` (dann hex dekodieren)
        for line in r.stderr.splitlines():
            if line.startswith("password: 0x"):
                return bytes.fromhex(line[12:].split()[0]).decode("utf-8")
            if line.startswith('password: "'):
                return line[11:-1]
        return None
    if IS_WIN:
        pcred = ctypes.POINTER(_CRED)()
        if not _advapi.CredReadW(f"{KEYCHAIN_SERVICE}:{account}", _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
            return None
        try:
            c = pcred.contents
            return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
        finally:
            _advapi.CredFree(pcred)
    try:
        return json.loads(_secret_file().read_text(encoding="utf-8")).get(account)
    except (OSError, ValueError):
        return None


def keychain_set(account: str, secret: str) -> None:
    if not secret or any(c in secret + account for c in '"\n\r'):
        raise SyncError("Leerer Wert oder ungültige Zeichen im Zugangsschlüssel.")
    if IS_MAC:
        # Über `security -i` (stdin), damit das Geheimnis nicht in der Prozessliste steht.
        cmd = f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{account}" -w "{secret}"\n'
        r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
        if r.returncode != 0 or "error" in (r.stderr + r.stdout).lower():
            raise SyncError("Konnte nicht im Schlüsselbund speichern: " + (r.stderr or r.stdout).strip())
        return
    if IS_WIN:
        blob = secret.encode("utf-8")
        if len(blob) > 2560:
            raise SyncError("Geheimnis ist zu lang für die Windows-Anmeldeinformationsverwaltung.")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = _CRED(Type=_CRED_TYPE_GENERIC, TargetName=f"{KEYCHAIN_SERVICE}:{account}", UserName=account,
                     CredentialBlobSize=len(blob), CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
                     Persist=_CRED_PERSIST_LOCAL_MACHINE)
        if not _advapi.CredWriteW(ctypes.byref(cred), 0):
            raise SyncError(f"Konnte nicht in der Anmeldeinformationsverwaltung speichern (Fehler {ctypes.get_last_error()}).")
        return
    f = _secret_file()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[account] = secret
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def keychain_delete(account: str) -> None:
    if IS_MAC:
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
                       capture_output=True)
    elif IS_WIN:
        _advapi.CredDeleteW(f"{KEYCHAIN_SERVICE}:{account}", _CRED_TYPE_GENERIC, 0)
    else:
        try:
            data = json.loads(_secret_file().read_text(encoding="utf-8"))
            data.pop(account, None)
            _secret_file().write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError):
            pass


_BAD = re.compile(r'[\x00-\x1f/\\:*?"<>|]')


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str, fallback: str = "unbenannt") -> str:
    """Datei-/Ordnername, der auf macOS und Windows gültig ist (gleiche Namen auf beiden Systemen)."""
    name = html.unescape(name)
    name = _BAD.sub("_", name).strip(" .")[:150].rstrip(" .")
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name or fallback


def safe_join(base: Path, *parts: str) -> Path:
    p = base
    for part in parts:
        for seg in part.split("/"):
            if seg in ("", ".", ".."):
                continue
            p = p / safe_name(seg)
    base_r, p_r = base.resolve(), p.resolve()
    if base_r != p_r and base_r not in p_r.parents:
        raise SyncError(f"Unsicherer Pfad: {p}")
    return p


def add_query(url: str, **params: str) -> str:
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if k not in params] + list(params.items())
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))


def filename_from_response(resp, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        return urllib.parse.unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        name = m.group(1)
        try:  # HTTP-Header kommen als Latin-1 an, sind aber oft UTF-8
            name = name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return urllib.parse.unquote(name)
    path = urllib.parse.urlsplit(resp.geturl()).path
    return urllib.parse.unquote(path.rsplit("/", 1)[-1]) or fallback


def save_stream(resp, dest: Path, mtime: int | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with tmp.open("wb") as f:
        while chunk := resp.read(1 << 16):
            f.write(chunk)
    tmp.replace(dest)
    if mtime:
        os.utime(dest, (mtime, mtime))


_NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: kein aufblitzendes Konsolenfenster

_WIN_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode('Moodle-Sync')) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:MOODLE_SYNC_MSG)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(msg: str) -> None:
    """Mitteilung; der Text geht als Argument bzw. Umgebungsvariable rein, nie in den Skripttext."""
    try:
        if IS_MAC:
            subprocess.run(["osascript", "-e", "on run argv", "-e",
                            'display notification (item 1 of argv) with title "Moodle-Sync"', "-e", "end run", msg],
                           capture_output=True, timeout=20)
        elif IS_WIN:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_TOAST],
                           env={**os.environ, "MOODLE_SYNC_MSG": msg}, capture_output=True,
                           creationflags=_NO_WINDOW, timeout=30)
        else:
            subprocess.run(["notify-send", "Moodle-Sync", msg], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def open_target(target: str) -> None:
    """Ordner im Finder/Explorer oder Adresse im Browser öffnen."""
    if IS_WIN:
        os.startfile(target)  # noqa: S606 – nur eigene Ordner und die Moodle-Adresse
    elif IS_MAC:
        subprocess.run(["open", target], capture_output=True)
    else:
        subprocess.run(["xdg-open", target], capture_output=True)


_PICK_TK = """
import sys, tkinter
from tkinter import filedialog
r = tkinter.Tk(); r.withdraw(); r.attributes("-topmost", True)
p = filedialog.askdirectory(initialdir=sys.argv[1], title="Zielordner wählen", parent=r)
sys.stdout.write(p or "")
"""


PICK_HOOK = None  # die App setzt hier ihren eigenen Ordner-Dialog ein


def pick_folder(start: str) -> str | None:
    """Ordnerauswahl des Systems. Gibt None zurück, wenn abgebrochen."""
    if PICK_HOOK:
        out = PICK_HOOK(start)
        return str(Path(out)) if out else None
    if IS_MAC:
        script = ('set p to POSIX path of (choose folder with prompt "Zielordner wählen" '
                  f'default location (POSIX file {json.dumps(start)}))')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        out = r.stdout.strip() if r.returncode == 0 else ""
    else:
        r = subprocess.run([sys.executable, "-I", "-c", _PICK_TK, start], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise SyncError("Ordnerauswahl geht hier nicht (tkinter fehlt?) – bitte den Pfad eintippen.")
        out = r.stdout.strip()
    if not out:
        return None
    return str(Path(out))


def is_path_label(label: str) -> bool:
    """Kurs-Eintrag ist ein Pfad (~/…, /…, C:\\…) und kein Name unter target?"""
    return label.startswith("~") or os.path.isabs(label)


# ---------------------------------------------------------------- token mode (API)

class TokenClient:
    def __init__(self, site: dict):
        self.site = site
        self.token = keychain_get(f"{site['name']}-token")
        if not self.token:
            raise SyncError(f"Kein Token für '{site['name']}'. Erst: moodle-sync login {site['name']}")
        self.opener = urllib.request.build_opener()
        self.opener.addheaders = [("User-Agent", UA)]

    def call(self, fn: str, **args) -> dict | list:
        data = {"wstoken": self.token, "wsfunction": fn, "moodlewsrestformat": "json"}
        for k, v in args.items():
            data[k] = str(v)
        body = urllib.parse.urlencode(data).encode()
        with self.opener.open(f"{self.site['url']}/webservice/rest/server.php", body, timeout=60) as r:
            res = json.load(r)
        if isinstance(res, dict) and res.get("exception"):
            if res.get("errorcode") == "invalidtoken":
                raise SyncError(f"Token für '{self.site['name']}' ungültig – moodle-sync login {self.site['name']} ausführen.")
            raise SyncError(f"{fn}: {res.get('message') or res.get('errorcode')}")
        time.sleep(DELAY)
        return res

    def courses(self) -> list[tuple[int, str]]:
        uid = self.call("core_webservice_get_site_info")["userid"]
        cs = self.call("core_enrol_get_users_courses", userid=uid)
        return [(c["id"], c["fullname"]) for c in cs]

    def sync_course(self, cid: int, dest: Path) -> tuple[int, int, int]:
        new = skipped = errors = 0
        sections = self.call("core_course_get_contents", courseid=cid)
        # Unterabschnitte (Moodle 4.5, z. B. Woche → Vorlesung/Übung) kommen als eigene
        # Abschnitte am Ende. Sie werden im Ordner ihres Elternabschnitts verschachtelt.
        top, parent = 0, {}
        dirs: dict[int, str] = {}
        for si, sec in enumerate(sections):
            if sec.get("component") == "mod_subsection":
                continue
            dirs[si] = f"{top:02d} {safe_name(sec.get('name') or 'Abschnitt')}"
            top += 1
            for mod in sec.get("modules", []):
                if mod.get("modname") == "subsection":
                    parent[mod.get("instance")] = dirs[si]
        for si, sec in enumerate(sections):
            if si not in dirs:
                name = safe_name(sec.get("name") or "Abschnitt")
                p = parent.get(sec.get("itemid"))
                dirs[si] = f"{p}/{name}" if p else f"{top:02d} {name}"
                top += 0 if p else 1
        for si, sec in enumerate(sections):
            sec_dir = dirs[si]
            for mod in sec.get("modules", []):
                if mod.get("modname") == "url":
                    link = ((mod.get("contents") or [{}])[0]).get("fileurl", "")
                    r = self.sciebo(link, safe_join(dest, sec_dir), dest) or download_note(
                        link, safe_join(dest, sec_dir), dest, self.site.get("notes_hosts", []))
                    new += r == "new"
                    skipped += r == "skip"
                    errors += r == "err"
                    continue
                files = [c for c in mod.get("contents") or [] if c.get("type") == "file"]
                if not files:
                    continue
                # Ordner-Aktivitäten und Aktivitäten mit mehreren Dateien bekommen einen Unterordner
                mod_dir = mod["name"] if (mod.get("modname") == "folder" or len(files) > 1) else ""
                for f in files:
                    if skip_file(f["filename"]):
                        continue
                    fname = f["filename"]
                    if mod.get("modname") == "page" and fname == "index.html" and not mod_dir:
                        fname = f"{mod['name']}.html"  # Moodle-Seite: sprechender Name statt index.html
                    path = safe_join(dest, sec_dir, mod_dir, f.get("filepath", "/"), fname)
                    size, mtime = f.get("filesize"), f.get("timemodified")
                    if path.exists():
                        st = path.stat()
                        # Seiten melden Größe 0 – dann zählt nur das Datum
                        if (st.st_size == size or not size) and (not mtime or int(st.st_mtime) == mtime):
                            skipped += 1
                            continue
                    url = add_query(f["fileurl"], token=self.token)
                    try:
                        with self.opener.open(url, timeout=300) as r:
                            save_stream(r, path, mtime)
                    except (OSError, urllib.error.URLError) as e:  # Timeout, Abbruch …
                        print(f"  ! {path.relative_to(dest)}: {e} – nächster Lauf versucht es erneut")
                        path.with_name(path.name + ".part").unlink(missing_ok=True)
                        errors += 1
                        continue
                    print(f"  + {path.relative_to(dest)}")
                    new += 1
                    time.sleep(DELAY)
        return new, skipped, errors

    def sciebo(self, link: str, dest_dir: Path, base: Path) -> str:
        """Öffentliche Sciebo-Freigabe laden. Gibt 'new', 'skip' oder '' zurück."""
        return download_share(link, dest_dir, base, self.site.get("sciebo_max_mb", 300))


_SCIEBO = re.compile(r"^https://([\w-]+\.)?sciebo\.de/s/([A-Za-z0-9]+)")


def download_share(link: str, dest_dir: Path, base: Path, max_mb: int) -> str:
    m = _SCIEBO.match(link or "")
    if not m:
        return ""
    url = f"https://{m.group(1) or ''}sciebo.de/s/{m.group(2)}/download"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        r = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        print(f"  ! Sciebo {link} nicht ladbar (HTTP {e.code}, evtl. Passwort/abgelaufen)")
        return ""
    except (urllib.error.URLError, OSError) as e:
        print(f"  ! Sciebo {link}: {e} – nächster Lauf versucht es erneut")
        return "err"
    try:
        if "text/html" in r.headers.get("Content-Type", ""):
            print(f"  ! Sciebo {link} ist keine Datei (evtl. Passwortschutz) – übersprungen")
            return ""
        length = int(r.headers.get("Content-Length") or 0)
        name = filename_from_response(r, f"sciebo-{m.group(2)}")
        if skip_file(name):
            return ""
        path = safe_join(dest_dir, name)
        if path.exists() and (not length or path.stat().st_size == length):
            return "skip"
        if length > max_mb * 1024 * 1024:
            print(f"  ! {name}: {length // 2**20} MB > {max_mb} MB – übersprungen (sciebo_max_mb)")
            return ""
        try:
            save_stream(r, path)
        except OSError as e:
            print(f"  ! {name}: {e} – nächster Lauf versucht es erneut")
            path.with_name(path.name + ".part").unlink(missing_ok=True)
            return "err"
        print(f"  + {path.relative_to(base)}  (Sciebo)")
        time.sleep(DELAY)
        return "new"
    finally:
        r.close()


def download_note(link: str, dest_dir: Path, base: Path, hosts: list[str]) -> str:
    """Veröffentlichte CodiMD/HedgeDoc-Notiz als .md laden (z. B. Übungsblätter).

    Notizen werden oft nachträglich geändert: Bei anderem Inhalt wird überschrieben.
    """
    u = urllib.parse.urlsplit(link or "")
    m = re.fullmatch(r"/(?:s/)?([\w-]+)/?", u.path)
    if u.scheme != "https" or u.hostname not in hosts or not m:
        return ""
    req = urllib.request.Request(f"https://{u.hostname}/s/{m.group(1)}/download",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if not r.headers.get("Content-Type", "").startswith("text/markdown"):
                print(f"  ! Notiz {link} ist kein Markdown – übersprungen")
                return ""
            name = filename_from_response(r, f"{m.group(1)}.md")
            data = r.read()
    except urllib.error.HTTPError as e:
        print(f"  ! Notiz {link} nicht ladbar (HTTP {e.code})")
        return ""
    except (urllib.error.URLError, OSError) as e:
        print(f"  ! Notiz {link}: {e} – nächster Lauf versucht es erneut")
        return "err"
    if not name.endswith(".md"):
        name += ".md"
    path = safe_join(dest_dir, name)
    if path.exists() and path.read_bytes() == data:
        return "skip"
    changed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    print(f"  {'~' if changed else '+'} {path.relative_to(base)}  (Notiz{', aktualisiert' if changed else ''})")
    time.sleep(DELAY)
    return "new"


def setup_token(site: dict) -> None:
    print(f"Anmeldung bei {site['url']} (das Passwort wird nicht gespeichert, nur der Token).")
    user = input("Benutzername: ").strip()
    pw = getpass.getpass("Passwort: ")
    login_token(site, user, pw)
    print("Token im Schlüsselbund gespeichert.")


def login_token(site: dict, user: str, pw: str) -> None:
    """Holt mit Benutzername/Passwort einen Token und speichert nur den Token."""
    body = urllib.parse.urlencode(
        {"username": user, "password": pw, "service": "moodle_mobile_app"}
    ).encode()
    req = urllib.request.Request(f"{site['url']}/login/token.php", body, {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)
    if "token" not in res:
        raise SyncError(f"Login fehlgeschlagen: {res.get('error') or res}")
    keychain_set(f"{site['name']}-token", res["token"])


# ---------------------------------------------------------------- cookie mode (scraping)

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.h2: list[str] = []
        self._in_h2 = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for key in ("href", "src", "data"):
            if a.get(key):
                self.links.append(a[key])
        if tag == "h2":
            self._in_h2 = True

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_h2 = False

    def handle_data(self, data):
        if self._in_h2 and data.strip():
            self.h2.append(data.strip())


class HashIndex:
    """Prüfsummen aller Dateien unter einem Ordner (mit Cache), für 'schon einsortiert?'."""

    CACHE = DATA / ".hashcache.json"

    def __init__(self, root: Path):
        self.root = root
        try:
            cache = json.loads(self.CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        self.hashes: set[str] = set()
        fresh = {}
        for f in root.rglob("*"):
            if not f.is_file() or f.name == ".DS_Store" or f.name.endswith(".part"):
                continue
            if "Recovery-Code" in f.name or f.suffix in (".pem", ".key"):
                continue  # Geheimnisse nie lesen
            st = f.stat()
            key, sig = str(f), [st.st_size, int(st.st_mtime)]
            if cache.get(key, [None])[:2] == sig:
                digest = cache[key][2]
            else:
                digest = sha256_file(f)
            fresh[key] = sig + [digest]
            self.hashes.add(digest)
        self.CACHE.write_text(json.dumps(fresh), encoding="utf-8")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class CookieClient:
    def __init__(self, site: dict):
        self.site = site
        self._index: HashIndex | None = None
        raw = keychain_get(f"{site['name']}-cookie")
        if not raw:
            raise SyncError(f"Kein Cookie für '{site['name']}'. Erst: moodle-sync login {site['name']}")
        self.opener = urllib.request.build_opener()
        self.opener.addheaders = [("User-Agent", UA), ("Cookie", raw)]
        self.sesskey = None

    @property
    def index(self) -> HashIndex | None:
        # Erst beim Sync bauen: Das Hashen des ganzen Ordners dauert (iCloud)
        root = self.site.get("dedupe_root")
        if root and self._index is None:
            self._index = HashIndex(Path(os.path.expanduser(root)))
        return self._index

    def get(self, url: str):
        r = self.opener.open(url, timeout=120)
        time.sleep(DELAY)
        return r

    def page(self, url: str) -> tuple[str, str]:
        with self.get(url) as r:
            return r.geturl(), r.read().decode("utf-8", "replace")

    def check_session(self) -> None:
        final, body = self.page(f"{self.site['url']}/my/")
        m = re.search(r'"sesskey":"([^"]+)"', body)
        if "/my" not in urllib.parse.urlsplit(final).path or not m:
            raise SessionExpired(
                f"Session für '{self.site['name']}' abgelaufen – "
                f"moodle-sync {self.site['name']} im Terminal starten."
            )
        self.sesskey = m.group(1)

    def courses(self) -> list[tuple[int, str]]:
        self.check_session()
        payload = [{
            "index": 0,
            "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
            "args": {"offset": 0, "limit": 0, "classification": "all", "sort": "fullname"},
        }]
        url = f"{self.site['url']}/lib/ajax/service.php?sesskey={self.sesskey}"
        req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        with self.opener.open(req, timeout=60) as r:
            res = json.load(r)[0]
        if res.get("error"):
            raise SyncError(f"Kursliste: {res.get('exception', {}).get('message', res)}")
        return [(c["id"], html.unescape(c["fullname"])) for c in res["data"]["courses"]]

    def _links(self, body: str) -> LinkParser:
        p = LinkParser()
        p.feed(body)
        return p

    def _is_html(self, r) -> bool:
        return "text/html" in r.headers.get("Content-Type", "")

    def _download(self, url: str, dest_dir: Path, base: Path, fallback: str) -> str:
        """Lädt url nach dest_dir; gibt 'new', 'skip' oder 'html' zurück."""
        r = self.get(url)
        try:
            if self._is_html(r):
                return "html"
            path = safe_join(dest_dir, filename_from_response(r, fallback))
            length = r.headers.get("Content-Length")
            if skip_file(path.name):
                return ""
            if path.exists() and length and path.stat().st_size == int(length):
                return "skip"
            if self.index is None:
                save_stream(r, path)
            else:
                # Erst laden, dann prüfen: liegt die Datei (evtl. umbenannt) schon irgendwo einsortiert?
                tmp = Path(str(self.index.root)) / f".moodle-sync-{os.getpid()}.part"
                save_stream(r, tmp)
                digest = sha256_file(tmp)
                if digest in self.index.hashes:
                    tmp.unlink()
                    return "skip"
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp.replace(path)
                self.index.hashes.add(digest)
            print(f"  + {path.relative_to(base)}")
            return "new"
        finally:
            r.close()

    def _sections(self, body: str) -> list[tuple[str, str]]:
        """Teilt die Kursseite in Abschnitte: [(Ordnername, HTML des Abschnitts)]."""
        starts = [m.start() for m in re.finditer(r'<li[^>]+id="section-\d+"', body)]
        if not starts:
            return [("", body)]
        out = []
        for idx, a in enumerate(starts):
            seg = body[a:starts[idx + 1] if idx + 1 < len(starts) else len(body)]
            m = (re.search(r'data-sectionname="([^"]*)"', seg)
                 or re.search(r'<h3[^>]*sectionname[^>]*>(.*?)</h3>', seg, re.S))
            name = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""
            out.append((f"{idx:02d} {safe_name(' '.join(name.split()) or 'Abschnitt')}", seg))
        return out

    def sync_course(self, cid: int, dest: Path) -> tuple[int, int, int]:
        self.check_session()
        base = self.site["url"]
        new = skipped = 0
        _, body = self.page(f"{base}/course/view.php?id={cid}")

        def absol(u: str) -> str:
            return urllib.parse.urljoin(base + "/", html.unescape(u))

        def tally(result: str):
            nonlocal new, skipped
            if result == "new":
                new += 1
            elif result == "skip":
                skipped += 1

        seen: set[str] = set()
        for sec_dir, seg in self._sections(body):
            target = safe_join(dest, sec_dir)
            res_ids, folder_ids, direct = [], [], []
            for raw in self._links(seg).links:
                u = absol(raw)
                if not u.startswith(base):
                    continue
                if m := re.search(r"/mod/resource/view\.php\?id=(\d+)", u):
                    key, lst, val = "r" + m.group(1), res_ids, m.group(1)
                elif m := re.search(r"/mod/folder/view\.php\?id=(\d+)", u):
                    key, lst, val = "f" + m.group(1), folder_ids, m.group(1)
                elif ("/pluginfile.php/" in u and "/mod_" in u
                      and not re.search(r"\.(png|jpe?g|gif|svg|ico|webp)(\?|$)", u, re.I)):
                    # Dateien aus Textfeldern; Logos, Theme-Bilder und Deko-Grafiken nicht
                    key = val = u.split("#")[0]
                    lst = direct
                else:
                    continue
                if key not in seen:
                    seen.add(key)
                    lst.append(val)

            for rid in res_ids:
                result = self._download(f"{base}/mod/resource/view.php?id={rid}&redirect=1", target, dest, f"datei-{rid}")
                if result == "html":
                    # Eingebettete Anzeige: pluginfile-Link aus der Seite holen
                    _, pbody = self.page(f"{base}/mod/resource/view.php?id={rid}")
                    for raw in self._links(pbody).links:
                        u = absol(raw)
                        if "/pluginfile.php/" in u and "/mod_resource/content/" in u:
                            tally(self._download(add_query(u, forcedownload="1"), target, dest, f"datei-{rid}"))
                            break
                else:
                    tally(result)

            for fid in folder_ids:
                _, fbody = self.page(f"{base}/mod/folder/view.php?id={fid}")
                p = self._links(fbody)
                title = p.h2[0] if p.h2 else f"ordner-{fid}"
                fseen = set()
                for raw in p.links:
                    u = absol(raw).split("#")[0]
                    if "/pluginfile.php/" not in u or "/mod_folder/content/" not in u or u in fseen:
                        continue
                    fseen.add(u)
                    # Unterordner aus der URL übernehmen: .../mod_folder/content/<rev>/<pfad>/<datei>
                    tail = urllib.parse.unquote(urllib.parse.urlsplit(u).path.split("/mod_folder/content/", 1)[1])
                    sub = "/".join(tail.split("/")[1:-1])
                    tally(self._download(add_query(u, forcedownload="1"), safe_join(target, title, sub), dest, "datei"))

            for u in direct:
                tally(self._download(u, target, dest, "datei"))

        return new, skipped, 0


def setup_cookie(site: dict) -> None:
    print(f"""Cookie für {site['url']} einrichten:
  1. Im Browser bei {site['url']} einloggen.
  2. Entwicklertools öffnen (Cmd+Alt+I) -> Application/Speicher -> Cookies -> {site['url']}
  3. Den Cookie, dessen Name mit 'MoodleSession' beginnt, als  Name=Wert  einfügen.
     (Nur den Wert einfügen geht auch, dann wird der Name aus config.toml benutzt.)""")
    store_cookie(site, getpass.getpass("Cookie (Eingabe unsichtbar): "))
    print("Cookie gespeichert, Session ist gültig.")


def store_cookie(site: dict, raw: str) -> None:
    """Prüft den MoodleSession-Cookie, speichert ihn und testet die Session."""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if "=" not in raw:
        raw = f"{site.get('cookie_name', 'MoodleSession')}={raw}"
    if not re.fullmatch(r"MoodleSession\w*=[\w%-]+", raw):
        raise SyncError("Das sieht nicht wie ein MoodleSession-Cookie aus.")
    keychain_set(f"{site['name']}-cookie", raw)
    CookieClient(site).check_session()


# ---------------------------------------------------------------- CLI

def client(site: dict):
    return TokenClient(site) if site["mode"] == "token" else CookieClient(site)


def cmd_setup(cfg: dict, name: str) -> None:
    site = site_cfg(cfg, name)
    (setup_token if site["mode"] == "token" else setup_cookie)(site)


def cmd_courses(cfg: dict, name: str) -> None:
    site = site_cfg(cfg, name)
    for cid, title in client(site).courses():
        print(f"{cid:>8}  {title}")


def apply_skip_ext(cfg: dict) -> None:
    if "skip_ext" in cfg:
        SKIP_EXT.clear()
        SKIP_EXT.update(e.lower().lstrip(".") for e in cfg["skip_ext"])


def course_dest(site: dict, label: str, target: Path) -> tuple[Path, str]:
    # "~/…" oder "/…" = fester Zielordner, sonst <target>/<site>/<label>
    if is_path_label(label):
        dest = Path(os.path.expanduser(label))
        if dest.name == "Moodle":  # <Kursordner>/Moodle -> Name des Kursordners
            return dest, dest.parent.name
        if dest.name.startswith("Moodle "):  # <Kursordner>/Moodle AG -> "<Kursordner> – AG"
            return dest, f"{dest.parent.name} – {dest.name[7:]}"
        return dest, dest.name
    return safe_join(target, site["name"], label), label


def sync_site(site: dict, courses: dict, target: Path) -> int:
    """Synchronisiert alle Kurse einer Uni, schreibt das Ergebnis je Kurs in state.json.

    Gibt die Zahl der Dateifehler zurück. Harte Fehler (Login, Netz weg) werfen eine Exception.
    """
    name = site["name"]
    c = client(site)
    errors = 0
    for cid, label in courses.items():
        dest, label = course_dest(site, label, target)
        new, skipped, err = c.sync_course(int(cid), dest)
        errors += err
        st = load_state()
        st.setdefault("sites", {}).setdefault(name, {}).setdefault("courses", {})[str(cid)] = {
            "label": label, "path": str(dest), "new": new, "skipped": skipped,
            "errors": err, "time": int(time.time())}
        save_state(st)
        extra = f", {err} Fehler" if err else ""
        print(f"[{name}] {label}: {new} neu, {skipped} schon vorhanden{extra}")
    return errors


def run_site(cfg: dict, name: str, interactive: bool) -> tuple[bool, str | None, bool]:
    """Ein Lauf für eine Uni. Gibt (ok, fehlertext, offline) zurück und merkt sich das Ergebnis."""
    site = site_cfg(cfg, name)
    target = Path(os.path.expanduser(cfg.get("target", "~/Moodle")))
    courses = site.get("courses", {})
    if not courses:
        print(f"[{name}] keine Kurse ausgewählt – überspringe.")
        return True, None, False
    started = int(time.time())
    update_state(name, last_try=started, running=True)
    err, offline, expired = None, False, False
    try:
        try:
            errors = sync_site(site, courses, target)
        except SessionExpired:
            if not interactive:
                raise
            print(f"[{name}] Session abgelaufen – Browser öffnet sich, bitte einloggen.")
            open_target(site["url"] + "/my/")
            setup_cookie(site)
            errors = sync_site(site, courses, target)
        if errors:
            err = f"{errors} Datei(en) nicht geladen – nächster Lauf versucht es erneut"
    except SessionExpired as e:
        err, expired = str(e), True
    except (SyncError, urllib.error.URLError, OSError) as e:
        err, offline = str(e), is_offline(e)
        if offline:
            err = "Keine Verbindung zu Moodle (offline?)"
    fields = {"running": False, "error": err, "expired": expired, "offline": offline}
    if err is None:
        fields["last_ok"] = started
    update_state(name, **fields)
    if err:
        print(f"[{name}] FEHLER: {err}", file=sys.stderr)
    return err is None, err, offline


def cmd_sync(cfg: dict, names: list[str], interactive: bool | None = None) -> int:
    apply_skip_ext(cfg)
    names = names or list(cfg.get("sites", {}))
    for n in names:
        site_cfg(cfg, n)  # unbekannte Namen früh melden
    with run_lock(wait=False) as got:
        if not got:
            print("Es läuft gerade schon ein Sync (Job oder Oberfläche). Bitte kurz warten.")
            return 1
        failed = 0
        if interactive is None:
            interactive = bool(sys.stdin and sys.stdin.isatty())
        for name in names:
            ok, err, _ = run_site(cfg, name, interactive=interactive)
            if not ok:
                failed += 1
                if not interactive:
                    notify(f"{name}: {err}"[:200].replace('"', "'"))
        return 1 if failed else 0


def cmd_auto(cfg: dict) -> int:
    """Vom Hintergrund-Job alle 15 Minuten und bei Netzwechsel aufgerufen.

    Lädt nur, wenn seit dem letzten geplanten Zeitpunkt (z. B. 8:00/18:00) noch kein
    erfolgreicher Lauf war. Offline: still beenden, beim nächsten Aufruf erneut.
    """
    apply_skip_ext(cfg)
    now = datetime.now()
    slot = int(last_slot(now, cfg.get("schedule", DEFAULT_SCHEDULE)).timestamp())
    st = load_state().get("sites", {})
    due = [n for n, s in cfg.get("sites", {}).items()
           if site_auto(s) and s.get("courses") and st.get(n, {}).get("last_ok", 0) < slot]
    if not due:
        return 0  # nichts fällig – kein Logeintrag
    with run_lock(wait=False) as got:
        if not got:
            return 0  # läuft schon (z. B. aus der Oberfläche)
        for name in due:
            site = site_cfg(cfg, name)
            if not online(site["url"]):
                update_state(name, offline=True, last_check=int(time.time()))
                continue  # still: nächster Aufruf versucht es erneut
            print(f"== {datetime.now():%d.%m.%Y %H:%M} automatischer Lauf: {name}")
            ok, err, offline = run_site(cfg, name, interactive=False)
            if not ok and not offline and st.get(name, {}).get("notified_slot") != slot:
                notify(f"{name}: {err}"[:200])  # einmal pro Termin
                update_state(name, notified_slot=slot)
    return 0


# ---------------------------------------------------------------- Hintergrund-Job (launchd bzw. Windows-Aufgabenplanung)

def job_plist() -> str:
    from xml.sax.saxutils import escape as x
    args = self_cmd("auto")
    arr = "\n".join(f"    <string>{x(a)}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{JOB_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{arr}
  </array>
  <!-- 'auto' lädt nur, wenn ein Termin aus config.toml (schedule) noch offen ist.
       Geprüft wird beim Anmelden, alle 15 Minuten und bei Netzwechsel (WLAN an/aus). -->
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>900</integer>
  <key>WatchPaths</key>
  <array>
    <string>/private/var/run/resolv.conf</string>
  </array>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{x(str(LOG))}</string>
  <key>StandardErrorPath</key><string>{x(str(LOG))}</string>
</dict>
</plist>
"""


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _mac_job_status() -> dict:
    r = _launchctl("print", f"gui/{os.getuid()}/{JOB_LABEL}")
    info = {"installed": JOB_PLIST.exists(), "loaded": r.returncode == 0, "uses_auto": False}
    if r.returncode == 0:
        if m := re.search(r"last exit code = (.+)", r.stdout):
            info["last_exit"] = m.group(1)
        if m := re.search(r"\bruns = (\d+)", r.stdout):
            info["runs"] = int(m.group(1))
        info["running"] = "state = running" in r.stdout
    if JOB_PLIST.exists():
        info["uses_auto"] = "<string>auto</string>" in JOB_PLIST.read_text(encoding="utf-8")
    return info


def _mac_job_enable() -> None:
    JOB_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"gui/{os.getuid()}/{JOB_LABEL}")
    JOB_PLIST.write_text(job_plist(), encoding="utf-8")
    r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(JOB_PLIST))
    if r.returncode != 0:
        raise SyncError(f"Job konnte nicht gestartet werden: {r.stderr.strip()}")


def _mac_job_disable() -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{JOB_LABEL}")


def self_cmd(*args: str, gui: bool = False) -> list[str]:
    """Befehl, der dieses Programm startet – als App direkt, sonst über Python."""
    if FROZEN:
        return [sys.executable, *args]
    py = _pythonw() if gui or IS_WIN else _stable_python()
    return [py, "-I", "-X", "utf8", str(HERE / "moodle_sync.py"), *args]


def _pythonw() -> str:
    """Unter Windows pythonw.exe, damit alle 15 Minuten kein Konsolenfenster aufgeht."""
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w) if IS_WIN and w.exists() else sys.executable


def win_task_xml() -> str:
    from xml.sax.saxutils import escape as x
    user = x(f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\"))
    cmd = self_cmd("auto")
    args = subprocess.list2cmdline(cmd[1:])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Moodle-Sync: lädt zu den Terminen aus config.toml, holt Verpasstes nach.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>
    <TimeTrigger>
      <Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary><Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>{x(cmd[0])}</Command><Arguments>{x(args)}</Arguments><WorkingDirectory>{x(str(DATA))}</WorkingDirectory></Exec></Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, errors="replace",
                          creationflags=_NO_WINDOW)


def _win_job_status() -> dict:
    r = _schtasks("/Query", "/TN", WIN_TASK, "/XML")
    info = {"installed": r.returncode == 0, "loaded": False, "uses_auto": False}
    if r.returncode == 0:
        xml = r.stdout
        info["loaded"] = not re.search(r"<Settings>.*?<Enabled>false</Enabled>", xml, re.S)
        info["uses_auto"] = "auto" in (re.search(r"<Arguments>(.*?)</Arguments>", xml, re.S) or [""])[0]
        v = _schtasks("/Query", "/TN", WIN_TASK, "/V", "/FO", "CSV", "/NH")
        import csv
        rows = list(csv.reader(v.stdout.splitlines()))
        if rows and len(rows[0]) > 6:  # Spalte 7 = letztes Ergebnis (Spaltentitel sind übersetzt, die Reihenfolge nicht)
            info["last_exit"] = rows[0][6]
    return info


def _win_job_enable() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    xml_file = DATA / "task.xml"
    xml_file.write_text(win_task_xml(), encoding="utf-16")
    r = _schtasks("/Create", "/TN", WIN_TASK, "/XML", str(xml_file), "/F")
    if r.returncode != 0:
        raise SyncError(f"Aufgabe konnte nicht angelegt werden: {(r.stderr or r.stdout).strip()}")


def _win_job_disable() -> None:
    _schtasks("/Delete", "/TN", WIN_TASK, "/F")


def job_status() -> dict:
    if IS_WIN:
        return _win_job_status()
    if IS_MAC:
        return _mac_job_status()
    return {"installed": False, "loaded": False, "uses_auto": False, "unsupported": True}


def job_enable() -> None:
    if IS_WIN:
        _win_job_enable()
    elif IS_MAC:
        _mac_job_enable()
    else:
        raise SyncError("Automatischer Job geht nur unter macOS und Windows. Unter Linux z. B. per cron: "
                        f"*/15 * * * * {' '.join(self_cmd('auto'))}")
    st = load_state()
    st["job_cmd"] = self_cmd("auto")
    save_state(st)


def job_refresh() -> bool:
    """Ist der Job an, zeigt aber auf einen alten Programmort (App verschoben/aktualisiert)? Dann neu anlegen."""
    if load_state().get("job_cmd") in (None, self_cmd("auto")) or not job_status().get("loaded"):
        return False
    job_enable()
    return True


def job_disable() -> None:
    if IS_WIN:
        return _win_job_disable()
    if IS_MAC:
        return _mac_job_disable()


def cmd_job(arg: str) -> int:
    if arg in ("an", "on"):
        job_enable()
        print("Job ist an: prüft alle 15 Minuten" + (" und bei Netzwechsel" if IS_MAC else "")
              + ", lädt zu den Terminen aus config.toml.")
    elif arg in ("aus", "off"):
        job_disable()
        print("Job ist aus ('moodle-sync job an' schaltet ihn wieder ein).")
    else:
        print(json.dumps(job_status(), indent=1))
    return 0


# ---------------------------------------------------------------- neue Uni erkennen

def probe_site(url: str) -> dict:
    """Prüft eine Moodle-URL: Mobile-API an (token) oder nur Browser-Login (cookie)?"""
    url = url.strip().rstrip("/")
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    if u.scheme != "https" or not u.hostname or "@" in u.netloc:
        raise SyncError("Bitte eine https-Adresse angeben, z. B. https://moodle.uni-x.de")
    base = f"https://{u.netloc.lower()}{u.path.rstrip('/')}"  # netloc behält einen eigenen Port
    payload = json.dumps([{"index": 0, "methodname": "tool_mobile_get_public_config", "args": {}}]).encode()
    req = urllib.request.Request(f"{base}/lib/ajax/service-nologin.php", payload,
                                 {"Content-Type": "application/json", "User-Agent": UA})
    info = {"url": base, "mode": "cookie", "sitename": u.hostname}
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.load(r)[0]
    except (urllib.error.URLError, ValueError, KeyError, IndexError) as e:
        raise SyncError(f"Keine Moodle-Seite unter {base} gefunden ({e}).") from None
    data = res.get("data") or {}
    if not res.get("error"):
        info["sitename"] = html.unescape(data.get("sitename") or info["sitename"])
        if data.get("enablemobilewebservice") and data.get("typeoflogin") == 1:
            info["mode"] = "token"
    if info["mode"] == "cookie":
        try:
            r = urllib.request.urlopen(urllib.request.Request(f"{base}/login/index.php",
                                                              headers={"User-Agent": UA}), timeout=20)
            names = re.findall(r"(MoodleSession\w*)=", " ".join(r.headers.get_all("Set-Cookie") or []))
            r.close()
            if names:
                info["cookie_name"] = names[0]
        except (urllib.error.URLError, OSError):
            pass
    return info


# ---------------------------------------------------------------- Einrichten / Entfernen

def _ps(script: str, **env: str) -> subprocess.CompletedProcess:
    """PowerShell-Befehl; Werte nur über Umgebungsvariablen, nie in den Skripttext."""
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          env={**os.environ, **env}, capture_output=True, text=True, errors="replace",
                          creationflags=_NO_WINDOW)


_WIN_LINKS = """
$sh = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
  if (-not $dir) { continue }
  $l = Join-Path $dir 'Moodle-Sync.lnk'
  if ($env:MS_REMOVE) { Remove-Item -LiteralPath $l -ErrorAction SilentlyContinue; continue }
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $s = $sh.CreateShortcut($l)
  $s.TargetPath = $env:MS_TARGET; $s.Arguments = $env:MS_ARGS; $s.WorkingDirectory = $env:MS_WD
  $s.Description = 'Moodle-Sync: Moodle-Kurse in eigene Ordner laden'
  $s.Save()
  Write-Output $l
}
"""


def _launchers() -> dict:
    """Wo Befehl und App-Starter liegen."""
    if IS_WIN:
        apps = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Microsoft/WindowsApps"
        return {"cmd": (apps if apps.is_dir() else DATA / "bin") / "moodle-sync.cmd"}
    out = {"cmd": Path.home() / ".local/bin/moodle-sync"}
    if IS_MAC:
        out["app"] = Path.home() / "Applications/Moodle-Sync.app"
    return out


def _stable_python() -> str:
    """Python-Pfad, der ein Update übersteht (z. B. /opt/homebrew/bin/python3 statt …/python@3.13/…)."""
    import shutil
    me = os.path.realpath(sys.executable)
    for c in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", shutil.which("python3"), shutil.which("python")):
        if c and os.path.realpath(c) == me:
            return c
    return sys.executable


def _ask(q: str) -> bool:
    try:
        return input(q + " [J/n] ").strip().lower() in ("", "j", "ja", "y", "yes")
    except EOFError:
        return False


APP_FILES = ("moodle_sync.py", "moodle_ui.py", "ui.html", "README.md", "config.example.toml",
             "install.sh", "uninstall.sh", "install.cmd", "uninstall.cmd")


def _app_dir() -> Path:
    """Fester Ort fürs Programm, damit es nach dem Löschen des Download-Ordners weiterläuft."""
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "moodle-sync/app"
    return DATA / "app"


def cmd_install(args: list[str]) -> int:
    if FROZEN:
        print("Die App braucht keine Installation. Den Hintergrund-Job schaltest du in der App unter „Zeitplan“ ein.")
        return 0
    if sys.version_info < (3, 9):
        raise SyncError("Python 3.9 oder neuer nötig.")
    # Entwicklungsordner (mit .gitignore) bleibt, wo er ist; ein entpacktes ZIP wird an den festen Ort kopiert
    if "--hier" not in args and not (HERE / ".gitignore").exists() and HERE != _app_dir():
        import shutil
        dest = _app_dir()
        dest.mkdir(parents=True, exist_ok=True)
        for n in APP_FILES:
            if (HERE / n).exists():
                shutil.copy2(HERE / n, dest / n)
        print(f"Programm kopiert nach {dest} (der entpackte Ordner kann danach weg).", flush=True)
        cmd = [sys.executable, "-I"] + (["-X", "utf8"] if IS_WIN else []) + [str(dest / "moodle_sync.py"), "install",
                                                                            "--hier", *args]
        return subprocess.run(cmd).returncode
    script = HERE / "moodle_sync.py"
    L = _launchers()
    L["cmd"].parent.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        line = f'@"{sys.executable}" -I -X utf8 "{script}" %*\r\n'
        try:  # cmd.exe liest .cmd-Dateien in der OEM-Codepage (Umlaute im Benutzernamen!)
            L["cmd"].write_bytes(line.encode("oem"))
        except UnicodeEncodeError:
            L["cmd"].write_bytes(("@chcp 65001 >nul\r\n" + line).encode("utf-8"))
        r = _ps(_WIN_LINKS, MS_TARGET=_pythonw(), MS_ARGS=subprocess.list2cmdline(["-I", "-X", "utf8", str(script), "ui"]),
                MS_WD=str(DATA))
        print("Verknüpfungen (Startmenü, Desktop):", "angelegt" if r.returncode == 0 else f"FEHLER {r.stderr.strip()}")
    else:
        import shlex
        py = _stable_python()
        L["cmd"].write_text(f'#!/bin/sh\nexec {shlex.quote(py)} -I {shlex.quote(str(script))} "$@"\n',
                            encoding="utf-8")
        L["cmd"].chmod(0o755)
        if "app" in L:
            def aq(t: str) -> str:  # AppleScript-Zeichenkette
                return '"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"'
            src = (f"do shell script quoted form of {aq(py)} & \" -I \" & quoted form of {aq(str(script))}"
                   ' & " ui > /dev/null 2>&1 &"')
            L["app"].parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["osacompile", "-o", str(L["app"]), "-e", src], capture_output=True, text=True)
            print(f"App: {L['app']}", "" if r.returncode == 0 else f"– FEHLER {r.stderr.strip()}")
    print(f"Befehl: {L['cmd']}")
    if str(L["cmd"].parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"  Hinweis: {L['cmd'].parent} steht nicht im PATH. Im Terminal dann mit vollem Pfad aufrufen.")
    ensure_data()
    job = "--job" in args or ("--kein-job" not in args and sys.stdin and sys.stdin.isatty()
                              and _ask("Automatisch synchronisieren (Hintergrund-Job)?"))
    if job:
        job_enable()
        print("Hintergrund-Job ist an.")
    print(f"Einstellungen: {DATA}\nFertig. Starten: Moodle-Sync-" + ("Verknüpfung" if IS_WIN else "App")
          + " oder „moodle-sync ui“.")
    return 0


def cmd_uninstall(args: list[str]) -> int:
    job_disable()
    L = _launchers()
    if L["cmd"].exists():
        L["cmd"].unlink()
    if L.get("app") and L["app"].exists():
        import shutil
        shutil.rmtree(L["app"])
    if IS_WIN:
        _ps(_WIN_LINKS, MS_REMOVE="1")
    app = _app_dir()
    if app.is_dir() and HERE != app:  # kopiertes Programm entfernen (läuft man es selbst, bleibt es)
        for n in APP_FILES:
            try:
                (app / n).unlink()
            except OSError:
                pass
        import shutil
        shutil.rmtree(app / "__pycache__", ignore_errors=True)
        try:
            app.rmdir()
        except OSError:
            pass
    if "--alles" in args:
        try:
            for name, s in load_config().get("sites", {}).items():
                keychain_delete(f"{name}-{'token' if s.get('mode') == 'token' else 'cookie'}")
        except SyncError:
            pass
        for n in ("config.toml", "config.toml.bak", "config.toml.tmp", "state.json", "state.tmp", ".hashcache.json",
                  ".lock", ".secrets.json", "ui.json", "task.xml", "moodle-sync.log"):
            try:
                (DATA / n).unlink()
            except OSError:
                pass
        try:
            DATA.rmdir()  # nur wenn leer – fremde Dateien bleiben
        except OSError:
            pass
        print(f"Entfernt, auch Zugangsdaten und Einstellungen ({DATA}). Deine geladenen Dateien bleiben.")
    else:
        print(f"Entfernt. Einstellungen und Zugangsdaten bleiben ({DATA}); ganz weg mit „uninstall --alles“.")
    return 0


def _setup_output() -> None:
    """pythonw (Windows-Job) hat keine Konsole: Ausgaben ins Log. Nie an Umlauten scheitern."""
    if sys.stdout is None or sys.stderr is None:
        DATA.mkdir(parents=True, exist_ok=True)
        try:
            if LOG.stat().st_size > 2_000_000:  # Log klein halten
                LOG.write_bytes(LOG.read_bytes()[-1_000_000:])
        except OSError:
            pass
        f = open(LOG, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


ALIASES = {"login": "setup", "kurse": "courses", "oberfläche": "ui", "app": "ui",
           "installieren": "install", "deinstallieren": "uninstall"}


def _private(folder: Path) -> None:
    """Einstellungen, Stand und Protokoll nur für den eigenen Benutzer lesbar (macOS/Linux).
    Unter Windows schützen die Benutzerrechte von %APPDATA% bereits."""
    if IS_WIN:
        return
    try:
        os.chmod(folder, 0o700)
        for f in [*folder.iterdir(), LOG]:
            if f.is_file() and not f.is_symlink():
                os.chmod(f, 0o600)
    except OSError:
        pass


def ensure_data() -> None:
    """Legt den Datenordner an. Einmalig: alte Dateien aus dem Programmordner hierher kopieren (Originale bleiben)."""
    DATA.mkdir(parents=True, exist_ok=True)
    _private(DATA)
    if CONFIG.exists() or DATA == HERE or not (HERE / "config.toml").exists():
        return
    import shutil
    for n in ("config.toml", "state.json", ".hashcache.json"):
        if (HERE / n).exists():
            shutil.copy2(HERE / n, DATA / n)


def main(argv: list[str]) -> int:
    if not IS_WIN:
        os.umask(0o077)  # alles, was Moodle-Sync anlegt (Config, Stand, Log), nur für dich lesbar
    argv = [argv[0]] + [ALIASES.get(a, a) for a in argv[1:2]] + argv[2:]
    if len(argv) > 1 and argv[1] in ("-h", "--help", "hilfe"):
        print(__doc__)
        return 0
    _setup_output()
    ensure_data()
    try:
        if len(argv) > 1 and argv[1] == "install":
            return cmd_install(argv[2:])
        if len(argv) > 1 and argv[1] == "uninstall":
            return cmd_uninstall(argv[2:])
        if len(argv) > 1 and argv[1] == "ui":
            sys.path.insert(0, str(HERE))  # -I lässt den Skriptordner sonst weg
            import moodle_ui
            return moodle_ui.serve("--kein-browser" not in argv)
        if len(argv) > 1 and argv[1] == "job":
            return cmd_job(argv[2] if len(argv) > 2 else "status")
        cfg = load_config()
        if len(argv) > 1 and argv[1] == "auto":
            return cmd_auto(cfg)
        # "moodle-sync" oder "moodle-sync uni1 uni2" = sync
        if len(argv) == 1 or argv[1] in cfg.get("sites", {}):
            argv = [argv[0], "sync"] + argv[1:]
        if argv[1] not in ("setup", "courses", "sync"):
            print(__doc__)
            return 2
        if argv[1] == "sync":
            return cmd_sync(cfg, argv[2:])
        if len(argv) != 3:
            print(f"Benutzung: moodle-sync {argv[1]} <uni>")
            return 2
        {"setup": cmd_setup, "courses": cmd_courses}[argv[1]](cfg, argv[2])
        return 0
    except (SyncError, urllib.error.URLError) as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv))
