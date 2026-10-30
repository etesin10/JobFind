#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build the portable macOS distribution (PyInstaller onedir folder, not .app).
# Requires on the BUILD Mac only: Python 3.11 (python.org installer or Homebrew) + internet for pip.
# The resulting dist/JobSearchManager/ folder runs on Macs of the SAME architecture
# (Apple Silicon arm64 or Intel x86_64) WITHOUT Python installed.
#   Usage:  bash build_mac.sh            (uses python3.11)
#           PYTHON=/path/to/python3.11 bash build_mac.sh
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3.11}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "No se encontró $PY. Instale Python 3.11 (https://www.python.org/downloads/macos/) o use PYTHON=..." >&2
  exit 1
fi
"$PY" - <<'EOF'
import sys
if sys.version_info[:2] < (3, 11):
    sys.exit(f"Se requiere Python 3.11+, encontrado {sys.version.split()[0]}")
EOF

if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements-lock.txt

echo "Running tests..."
.venv/bin/python -m pytest -q -p no:cacheprovider

echo "Building executable..."
WORK="${TMPDIR:-/tmp}/jsh_pyinstaller_work"
.venv/bin/pyinstaller --noconfirm --clean --distpath dist --workpath "$WORK" packaging/JobSearchManager.spec

DIST=dist/JobSearchManager
for d in data excel config resumes exports backups logs source; do mkdir -p "$DIST/$d"; done
cp JOB_SEARCH_HARNESS_MASTER.md README.md INSTRUCCIONES.md AGENT_GUIDE.md "$DIST/"
cp "packaging/Iniciar JobSearchManager.command" "$DIST/"
cp packaging/dist_build_mac.sh "$DIST/build_mac.sh"
chmod +x "$DIST/JobSearchManager" "$DIST/Iniciar JobSearchManager.command" "$DIST/build_mac.sh"

# Reproducible source snapshot (no venv, no user data). Leading "/" anchors excludes to the project root,
# so package folders with the same name (e.g. jsh/excel) are NOT excluded.
rsync -a --delete \
  --exclude '/.venv' --exclude '/build' --exclude '/dist' --exclude '/data' --exclude '/excel' \
  --exclude '/exports' --exclude '/backups' --exclude '/logs' --exclude '/resumes' --exclude '/config' \
  --exclude '/.git' --exclude '.pytest_cache' --exclude '__pycache__' --exclude '/.nicegui' --exclude '.DS_Store' \
  ./ "$DIST/source/"
test -f "$DIST/source/jsh/excel/csv_import.py" || { echo "source snapshot incomplete" >&2; exit 1; }

# Zip with ditto: keeps symlinks + executable bits (Finder "Compress" and Windows zip tools may not).
ARCH="$(uname -m)"
ZIP="dist/JobSearchManager-macOS-${ARCH}.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$DIST" "$ZIP"

echo
echo "OK: $DIST/JobSearchManager  (arquitectura: $ARCH)"
echo "Zip para copiar a otra Mac: $ZIP"
