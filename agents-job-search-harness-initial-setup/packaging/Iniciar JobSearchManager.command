#!/bin/bash
# Double-click this file in Finder to start Job Search Harness on macOS.
# A Terminal window opens with the app log; close it (or press Ctrl+C) to stop the app.
cd "$(dirname "$0")" || exit 1

if [ ! -f ./JobSearchManager ]; then
  echo "No se encontró ./JobSearchManager junto a este archivo."
  echo "¿Copió la carpeta completa? Para Mac, descomprima JobSearchManager-macOS-*.zip con Finder."
  read -n 1 -s -r -p "Presione una tecla para cerrar…"
  exit 1
fi

# Copies made by some tools (Windows zip, some USB formats) drop the executable bit.
[ -x ./JobSearchManager ] || chmod +x ./JobSearchManager 2>/dev/null

# Downloaded copies carry the quarantine flag; macOS would block every bundled library one by one.
# You already approved this launcher, so clear the flag for THIS app's own files only (not your data).
if xattr -p com.apple.quarantine ./JobSearchManager >/dev/null 2>&1; then
  echo "Quitando la marca de cuarentena de la app (solo la primera vez)…"
  xattr -dr com.apple.quarantine ./JobSearchManager ./_internal 2>/dev/null
fi

./JobSearchManager "$@"
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "La app terminó con un error (código $status). Revise logs/jsh.log."
  read -n 1 -s -r -p "Presione una tecla para cerrar…"
fi
exit $status
