; Windows-Installer für Moodle-Sync (Inno Setup 6). Baut aus build\dist\Moodle-Sync (PyInstaller).
; Installiert nur für den aktuellen Benutzer, ohne Adminrechte.
#define AppVer GetEnv("MS_VERSION")

[Setup]
AppId={{8C1F3A52-6D0B-4E59-9A47-2F3B9D1E7C10}
AppName=Moodle-Sync
AppVersion={#AppVer}
AppVerName=Moodle-Sync {#AppVer}
DefaultDirName={localappdata}\Programs\Moodle-Sync
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=..\..\build\installer
OutputBaseFilename=Moodle-Sync-Setup-{#AppVer}
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\Moodle-Sync.exe
UninstallDisplayName=Moodle-Sync
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Symbol auf dem Desktop anlegen"

[Files]
Source: "..\..\build\dist\Moodle-Sync\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; alte Programmdateien einer früheren Version weg (Einstellungen liegen woanders und bleiben)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\Moodle-Sync"; Filename: "{app}\Moodle-Sync.exe"
Name: "{userdesktop}\Moodle-Sync"; Filename: "{app}\Moodle-Sync.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Moodle-Sync.exe"; Description: "Moodle-Sync jetzt starten"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Hintergrund-Job austragen, laufende App beenden. Einstellungen, Zugangsdaten und Dateien bleiben.
Filename: "{app}\Moodle-Sync.exe"; Parameters: "job aus"; Flags: runhidden waituntilterminated; RunOnceId: "JobAus"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM Moodle-Sync.exe"; Flags: runhidden; RunOnceId: "AppAus"
