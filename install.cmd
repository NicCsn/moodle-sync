@echo off
rem Moodle-Sync fuer Windows einrichten: Doppelklick auf diese Datei.
rem Sucht Python ab 3.9 und ruft "moodle_sync.py install" auf.
setlocal
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto found
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto found
echo Python fehlt. Bitte von https://www.python.org/downloads/ installieren
echo und dabei "Add python.exe to PATH" ankreuzen. Danach diese Datei erneut starten.
goto end
:found
%PY% -I -X utf8 "%~dp0moodle_sync.py" install %*
:end
echo.
pause
