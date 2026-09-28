#!/bin/bash
# Hoops hardened Nuitka build (primary operator artifact).
# Goal: compiled code hard to open back up. No .py in output.
# Usage: bash tools/k2b/build_nuitka.sh
set -e
# macOS/Linux have `python3`; Windows CI runners only ship `python`.
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
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
PYTHONPATH="$ROOT/games" "$PY" -m nuitka \
  --standalone --onefile \
  --static-libpython=no \
  --lto=yes \
  --python-flag=-OO \
  --assume-yes-for-downloads \
  --include-module=game_play.Play \
  --include-module=game_play.game_running \
  --include-module=game_play.game_util \
  --include-module=model.setting \
  --include-module=model.game \
  --include-module=model.group \
  --include-module=dbm.dumb \
  --include-module=dbm.gnu \
  --include-module=dbm.ndbm \
  --nofollow-import-to=tkinter,gui,gui2,ui_design,net,pygame,audio_play,moviepy,cv2,pynput,encryption,rsa,Crypto,mysql,numpy,PIL,serial,led \
  --output-dir=build-nuitka \
  --output-filename=hoops-api \
  api_main_freeze.py

echo "== 3. freeze supervisor (tiny, same hardening) =="
"$PY" -m nuitka \
  --standalone --onefile \
  --static-libpython=no \
  --lto=yes \
  --python-flag=-OO \
  --assume-yes-for-downloads \
  --output-dir=build-nuitka-supervisor \
  --output-filename=supervisor \
  tools/k2b/supervisor.py

echo "== 4. freeze bridge =="
"$PY" -m nuitka \
  --standalone --onefile \
  --static-libpython=no \
  --lto=yes \
  --python-flag=-OO \
  --assume-yes-for-downloads \
  --output-dir=build-nuitka-bridge \
  --output-filename=hoops-bridge \
  ws_bridge.py

echo "== 5. stage allowlist + deny .py =="
"$PY" tools/k2b/stage_allowlist.py --dist "$DIST" --out "$STAGE"
cp build-nuitka/hoops-api build-nuitka-bridge/hoops-bridge build-nuitka-supervisor/supervisor "$STAGE/"
echo "done: $STAGE"
