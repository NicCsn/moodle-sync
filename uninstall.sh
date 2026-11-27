#!/bin/sh
# Moodle-Sync entfernen: sh uninstall.sh   (mit --alles auch Einstellungen und Zugangsdaten)
# Deine heruntergeladenen Dateien bleiben immer erhalten.
cd "$(dirname "$0")" || exit 1
for py in python3 python3.13 python3.12 python3.11 /usr/bin/python3; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    exec "$py" -I "$(pwd)/moodle_sync.py" uninstall "$@"
  fi
done
echo "Python 3.9 oder neuer fehlt." >&2
exit 1
