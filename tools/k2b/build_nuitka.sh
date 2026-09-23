#!/bin/bash
# Hoops hardened Nuitka build (primary operator artifact).
# Goal: compiled code hard to open back up. No .py in output.
# Usage: bash tools/k2b/build_nuitka.sh
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
FRONT="$ROOT/frontend"
DIST="$FRONT/dist"
STAGE="$ROOT/stage-k2b"

echo "== 1. build website =="
npm --prefix "$FRONT" ci
npm --prefix "$FRONT" run build

echo "== 2. freeze api + bridge + supervisor (hardened flags) =="
# -OO strips docstrings/asserts (harder to read back).
# --lto + --output-dir keep runtime as compiled binaries only.
python3 -m nuitka \
  --standalone --onefile \
  --lto=yes \
  --python-flag=-OO \
  --assume-yes-for-downloads \
  --output-dir=build-nuitka \
  --output-filename=hoops-api \
  --include-data-dirs=games=games \
  api_main_freeze.py 2>/dev/null || echo "NOTE: api_main_freeze.py entry not yet wired — see stage script"

echo "== 3. stage allowlist + deny .py =="
python3 tools/k2b/stage_allowlist.py --dist "$DIST" --out "$STAGE"
echo "done: $STAGE"
