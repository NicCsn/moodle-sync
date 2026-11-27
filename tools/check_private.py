"""Prüft, dass nichts Persönliches ins Repository geht. Läuft vor jedem Commit (Hook) und auf GitHub.

Aufruf: python tools/check_private.py [--staged]
Eigene Namen, Hochschulen o. Ä. kommen in tools/private-words.txt (wird selbst nie committet).
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = {
    "Moodle-Sitzungscookie": r"MoodleSession\w*\s*=\s*[A-Za-z0-9%-]{12,}",
    "Moodle-Token": r"(?i)(wstoken|token)\s*[=:]\s*[\"']?[a-f0-9]{32}\b",
    "privater Schlüssel": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "GitHub-Token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "macOS-Benutzerpfad": r"/Users/(?!…|\.\.\.|<|\$|name\b|Shared\b)[A-Za-z0-9._-]+",
    "Windows-Benutzerpfad": r"(?i)C:\\\\?Users\\\\?(?!…|<|%|\.\.\.|name\b|Public\b)[A-Za-z0-9._-]+",
    "Linux-Benutzerpfad": r"/home/(?!…|<|\$|runner\b|name\b)[A-Za-z0-9._-]+",
    "E-Mail-Adresse": r"\b[A-Za-z0-9._%+-]+@(?!example\.|[a-z0-9-]+\.invalid\b|anthropic\.com)[A-Za-z0-9.-]+\.[a-z]{2,}\b",
}
BINARY = {".png", ".ico", ".icns", ".jpg", ".gif", ".pdf", ".zip", ".dmg", ".exe"}


def files(staged: bool) -> list[str]:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"] if staged else ["git", "ls-files"]
    return [f for f in subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).stdout.splitlines() if f]


def main() -> int:
    staged = "--staged" in sys.argv
    words_file = ROOT / "tools" / "private-words.txt"
    words = [w.strip() for w in words_file.read_text(encoding="utf-8").splitlines()
             if w.strip() and not w.startswith("#")] if words_file.exists() else []
    pats = dict(PATTERNS)
    for w in words:
        pats[f"privates Wort „{w[:2]}…“"] = r"(?i)\b" + re.escape(w) + r"\b"
    bad = []
    for f in files(staged):
        if f in ("tools/check_private.py", "tools/private-words.txt") or Path(f).suffix.lower() in BINARY:
            continue
        if f == "tools/private-words.txt":
            bad.append(f"{f}: darf nie committet werden")
            continue
        try:
            text = (subprocess.run(["git", "show", f":{f}"], cwd=ROOT, capture_output=True).stdout if staged
                    else (ROOT / f).read_bytes()).decode("utf-8", "replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for name, pat in pats.items():
                if re.search(pat, line):
                    bad.append(f"{f}:{i}: {name}")
    if subprocess.run(["git", "ls-files", "--error-unmatch", "tools/private-words.txt"], cwd=ROOT,
                      capture_output=True).returncode == 0:
        bad.append("tools/private-words.txt ist im Repository – sofort entfernen")
    if bad:
        print("Persönliche Daten gefunden – nichts committen:\n  " + "\n  ".join(bad))
        return 1
    print(f"Datenschutz-Prüfung ok ({len(pats)} Muster).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
