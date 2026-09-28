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
if [ "${K2B_SKIP_BUILD:-0}" = "1" ]; then
  # Fast iteration: reuse the dist folders from a previous full build.
  echo "K2B_SKIP_BUILD=1 -> reusing existing build-nuitka* dist folders"
else
npm --prefix "$FRONT" ci
npm --prefix "$FRONT" run build

echo "== 2. freeze api (hardened flags) =="
# -OO strips docstrings/asserts (harder to read back).
# Engine packages (game_play/model/...) are COMPILED IN via --include-module
# so no engine .py ships on disk. Never use --include-data-dirs=games
# (that would copy readable source next to the binary).
# Without the engine inside, games start then end instantly (no Play object).
PYTHONPATH="$ROOT/games" "$PY" -m nuitka \
  --standalone \
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
  --standalone \
  --static-libpython=no \
  --lto=yes \
  --python-flag=-OO \
  --assume-yes-for-downloads \
  --output-dir=build-nuitka-supervisor \
  --output-filename=supervisor \
  tools/k2b/supervisor.py

echo "== 4. freeze bridge =="
"$PY" -m nuitka \
  --standalone \
  --static-libpython=no \
  --lto=yes \
  --python-flag=-OO \
  --assume-yes-for-downloads \
  --output-dir=build-nuitka-bridge \
  --output-filename=hoops-bridge \
  ws_bridge.py
fi

echo "== 5. stage allowlist + deny .py =="
"$PY" tools/k2b/stage_allowlist.py --dist "$DIST" --out "$STAGE"

# --standalone (not --onefile): a onefile binary overwrites argv[0] and
# sys.executable with its temp unpack path, so it can never find the games/,
# ui/ and simulator/ folders that live next to it. It also re-unpacks the
# whole payload on every launch (~40s per binary on the floor machine).
# Each dist folder is copied in under its own name.
stage_dist() {  # $1 = build dir, $2 = exe base name, $3 = staged folder name
  # Nuitka names the dist folder after the SOURCE SCRIPT (api_main_freeze.dist,
  # ws_bridge.dist), not after --output-filename. So find the dist by looking
  # for the binary it actually contains instead of guessing the folder name.
  local src
  src=$(find "$1" -maxdepth 2 -type f \( -name "$2" -o -name "$2.exe" \) -path '*.dist/*' \
        -exec dirname {} \; 2>/dev/null | head -1)
  if [ -z "$src" ]; then
    echo "no dist folder containing $2 under $1"; exit 1
  fi
  rm -rf "${STAGE:?}/$3"
  cp -R "$src" "$STAGE/$3"
  echo "staged $3 <- $src"
}
stage_dist build-nuitka hoops-api hoops-api
stage_dist build-nuitka-bridge hoops-bridge hoops-bridge
stage_dist build-nuitka-supervisor supervisor supervisor

echo "done: $STAGE"
