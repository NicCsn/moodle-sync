# Moodle-Sync

Lädt die Dateien deiner Moodle-Kurse in Ordner deiner Wahl: Folien, Übungsblätter, Ordner, verlinkte Sciebo-Freigaben. Die Abschnitte und Wochen werden wie in Moodle als Ordner angelegt. Was schon da ist, wird übersprungen. Auf Wunsch läuft es zweimal am Tag im Hintergrund und holt verpasste Termine nach, sobald der Rechner wieder online ist.

Für **macOS** (ab 11, Apple-Chip) und **Windows** 10/11. Zugangsdaten liegen im macOS-Schlüsselbund bzw. in der Windows-Anmeldeinformationsverwaltung, nie in einer Datei.

## Installieren

Die fertigen Apps gibt es unter **Releases** (rechts auf der GitHub-Seite). Python braucht man dafür nicht.

**macOS:** `Moodle-Sync-<Version>.dmg` öffnen und **Moodle-Sync** auf „Programme“ ziehen. Beim ersten Start meldet macOS, dass die App nicht überprüft werden konnte (sie ist nicht bei Apple registriert). Dann: Systemeinstellungen → Datenschutz & Sicherheit → ganz unten „Dennoch öffnen“. Das ist nur einmal nötig.

**Windows:** `Moodle-Sync-Setup-<Version>.exe` ausführen. Warnt Windows („Der Computer wurde durch Windows geschützt“), auf „Weitere Informationen“ → „Trotzdem ausführen“ klicken. Installiert wird nur für dich, ohne Adminrechte. Danach gibt es **Moodle-Sync** im Startmenü und auf dem Desktop.

**Aktualisieren:** neue Version genauso installieren. Einstellungen, Kurse und Zugangsdaten bleiben.

<details>
<summary>Ohne App, als Skript (macOS mit Intel-Chip, Linux, Entwickler)</summary>

Braucht Python ab 3.9. Ordner herunterladen, dann `sh install.sh` (macOS/Linux) bzw. `install.cmd` (Windows). Das legt den Befehl `moodle-sync` und einen Starter an; die Oberfläche öffnet sich dann im Browser statt in einem eigenen Fenster.
</details>

## Benutzen

Moodle-Sync starten. Es öffnet sich ein Fenster, dazu kommt ein Symbol in die Menüleiste (macOS) bzw. in den Infobereich unten rechts (Windows). Darüber siehst du den letzten Stand, startest eine Synchronisation oder öffnest das Fenster wieder. Fenster schließen blendet es nur aus, „Beenden“ steht im Symbol-Menü.

1. Rechts **Moodle hinzufügen** und die Adresse deiner Uni eingeben, z. B. `https://moodle.uni-beispiel.de`.
2. **Anmelden.** Erlaubt die Uni die Moodle-App, reichen Anmeldename und Kennwort. Gespeichert wird nur der Zugangsschlüssel, nicht das Kennwort. Sonst wird die Browser-Sitzung übernommen (siehe unten).
3. **Kurse hinzufügen:** Kurse anhaken und für jeden den Zielordner wählen.
4. **Alle synchronisieren.**

Den Zielordner eines Kurses änderst du in der Kachel mit einem Klick auf den Ordnernamen. Bereits geladene Dateien bleiben im alten Ordner.

**Tipp für eigene Notizen:** Lass Moodle in einen Unterordner laden, z. B. `Mathe 1/Moodle`, und schreib deine Notizen daneben in `Mathe 1`. Gehören mehrere Moodle-Kurse zu einem Fach (Vorlesung und Übungsgruppe), nimm `Mathe 1/Moodle` und `Mathe 1/Moodle Übung`. Die Oberfläche zeigt dann beides in einer Kachel.

### Unis ohne App-Zugang (Browser-Sitzung)

Manche Unis schalten die Moodle-App-Schnittstelle ab. Dann gibt es nur den Weg über die Browser-Sitzung:

1. In der Oberfläche **Anmelden** und dann **Moodle im Browser öffnen**, dort normal einloggen.
2. Entwicklertools öffnen (Mac: Cmd + Alt + I, Windows: F12) → Application/Speicher → Cookies → den Wert von `MoodleSession…` kopieren und einfügen.

Die Sitzung läuft nach einiger Zeit ab. Dann steht bei der Uni „Sitzung abgelaufen“, und du wiederholst die Schritte. Solche Unis laufen standardmäßig nicht im Hintergrund, sondern nur, wenn du auf „Synchronisieren“ klickst.

## Was geladen wird und was nicht

- Dateien, Ordner und Seiten aus den Kursabschnitten, mit Unterabschnitten wie in Moodle (Woche → Vorlesung/Übung)
- Öffentliche **Sciebo**-Freigaben, die im Kurs verlinkt sind (Ordner als ZIP). Dateien über 300 MB werden übersprungen (einstellbar).
- Optional **CodiMD/HedgeDoc**-Notizen als `.md` (Server in den Einstellungen der Uni eintragen)
- **Nicht:** Videos (mp4, mkv … in „Zeitplan“ einstellbar), passwortgeschützte Freigaben, andere Webseiten

Geladene Dateien werden nie gelöscht. Ändert der Dozent eine Datei, wird sie beim nächsten Lauf ersetzt.

## Zeitplan

Standard: 8:00 und 18:00 Uhr. Der Hintergrund-Job schaut alle 15 Minuten nach (auf dem Mac auch bei jedem Netzwechsel). Ist ein Termin noch nicht erfolgreich gelaufen, lädt er nach. Ohne Netz wartet er still. Bei echten Fehlern (Sitzung abgelaufen, Zugang ungültig) gibt es eine Mitteilung.

## Terminal (Skript-Version)

| Befehl | Was passiert |
|---|---|
| `moodle-sync ui` | Oberfläche öffnen |
| `moodle-sync` | alle Unis laden |
| `moodle-sync <kurz>` | nur eine Uni laden (Kurzname aus der Oberfläche) |
| `moodle-sync kurse <kurz>` | eingeschriebene Kurse mit IDs anzeigen |
| `moodle-sync login <kurz>` | neu anmelden |
| `moodle-sync job an` / `aus` / `status` | Hintergrund-Job |
| `moodle-sync hilfe` | alle Befehle |

## Wo liegt was

| | macOS | Windows |
|---|---|---|
| Einstellungen (`config.toml`), Stand | `~/Library/Application Support/moodle-sync/` | `%APPDATA%\moodle-sync\` |
| Programm | `/Programme/Moodle-Sync.app` (Skript: `~/Library/Application Support/moodle-sync/app/`) | `%LOCALAPPDATA%\Programs\Moodle-Sync\` |
| Protokoll | `~/Library/Logs/moodle-sync.log` | `%APPDATA%\moodle-sync\moodle-sync.log` |
| Hintergrund-Job | `~/Library/LaunchAgents/de.local.moodle-sync.plist` | Aufgabenplanung: `moodle-sync` |
| Zugangsdaten | Schlüsselbund, Dienst `moodle-sync` | Anmeldeinformationsverwaltung, `moodle-sync:…` |

`config.toml` darf von Hand bearbeitet werden (Beispiel: `config.example.toml`). Die Oberfläche behält Kommentare bei und legt vorher `config.toml.bak` an.

## Aktualisieren

Neue Version entpacken und den Installer noch einmal ausführen. Einstellungen, Kurse und Zugangsdaten bleiben.

## Entfernen

App unter macOS: in „Zeitplan“ die automatische Synchronisation ausschalten, dann die App in den Papierkorb. Windows: Einstellungen → Apps → Moodle-Sync → Deinstallieren (trägt den Hintergrund-Job aus). Skript-Version: `sh uninstall.sh` bzw. `uninstall.cmd`. Einstellungen und Zugangsdaten bleiben. Mit `--alles` werden auch sie gelöscht. **Deine heruntergeladenen Dateien bleiben in jedem Fall.**

## Grenzen

- Windows: Sehr tiefe Ordner können an die Pfadgrenze von 260 Zeichen stoßen. Dann die Zielordner kürzer wählen oder in Windows „lange Pfade“ aktivieren.
- Die Apps sind nicht signiert (das kostet bei Apple und Microsoft Geld). Darum die einmalige Warnung beim ersten Start.
- Nutze das Programm nur für Kurse, in die du eingeschrieben bist, und im Rahmen der Nutzungsbedingungen deiner Uni. Es lädt mit kurzen Pausen, um den Server zu schonen.

## Für Entwickler

- `moodle_sync.py` (Logik und Befehle), `moodle_ui.py` (lokaler Server), `ui.html` (Oberfläche), `moodle_app.py` (Fenster und Symbol)
- Tests: `python -I tests/test_platform.py`. Auf GitHub laufen sie bei jedem Push auf echtem macOS und Windows.
- App lokal bauen: `pip install -r requirements-app.txt`, dann `pyinstaller --noconfirm moodle-sync.spec`
- Neue Version: `__version__` in `moodle_sync.py` erhöhen, committen, Tag `v<Version>` pushen. GitHub baut DMG und Setup und legt sie als Release ab.
