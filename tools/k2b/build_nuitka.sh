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

echo "== 2. freeze api (hardened flags) =="
# -OO strips docstrings/asserts (harder to read back).
# Engine packages (game_play/model/...) are COMPILED IN via --include-module
# so no engine .py ships on disk. Never use --include-data-dirs=games
# (that would copy readable source next to the binary).
# Without the engine inside, games start then end instantly (no Play object).
PYTHONPATH="$ROOT/games" python3 -m nuitka \
  --standalone --onefile \
  --static-libpython=no \
  --lto=yes \
  --python-flag=-OO \
  --assume-yes-for-downloads \
  --include-module=game_play.Play \
  --include-module=game_play.game_running \
  --include-module=game_play.game_util \
  --include-module=model.setting \
  --output-dir=build-nuitka \
  --output-filename=hoops-api \
  api_main_freeze.py

echo "== 3. stage allowlist + deny .py =="
python3 tools/k2b/stage_allowlist.py --dist "$DIST" --out "$STAGE"
echo "done: $STAGE"
