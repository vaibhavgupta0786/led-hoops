"""Hoops operator supervisor (frozen).
Spawns API + bridge next to the exe, opens Chrome kiosk, waits.
No source paths — uses exe folder as install root.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


def _looks_like_root(p: Path) -> bool:
    """The package root holds the data; the binary folders do not."""
    return (p / "games").is_dir() and (p / "ui").is_dir()


def install_root() -> Path:
    """Package root: the folder holding games/, ui/ and the three binaries.

    Built with --standalone, so sys.executable is this very binary and the
    root is its folder or its parent (each binary sits in its own subfolder).
    """
    cands = []
    env = os.getenv("INSTALL_ROOT")
    if env:
        cands.append(Path(env))
    exe = getattr(sys, "executable", "") or ""
    if exe:
        d = Path(exe).resolve().parent
        cands += [d, d.parent]
    if sys.argv and sys.argv[0]:
        d = Path(sys.argv[0]).resolve().parent
        cands += [d, d.parent]
    if not getattr(sys, "frozen", False):
        cands.append(Path(__file__).resolve().parent.parent.parent)

    uniq = []
    for c in cands:
        if c not in uniq:
            uniq.append(c)
    for c in uniq:
        if _looks_like_root(c):
            return c
    return uniq[0] if uniq else Path.cwd()


def wait_http(url: str, timeout: int = 60) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urlopen(url, timeout=3) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(1)
    return False


def main() -> int:
    root = install_root()
    api_port = int(os.getenv("API_PORT", "8000"))
    bridge_port = int(os.getenv("WS_BRIDGE_PORT", "8765"))

    suffix = ".exe" if os.name == "nt" else ""
    api_exe = root / "hoops-api" / ("hoops-api" + suffix)
    bridge_exe = root / "hoops-bridge" / ("hoops-bridge" + suffix)
    print(f"install root: {root}", flush=True)
    if not api_exe.exists():
        print(f"missing {api_exe} — reinstall operator zip", flush=True)
        return 1

    env = dict(os.environ, GAMES_ROOT=str(root / "games"), UI_DIR=str(root / "ui"),
               INSTALL_ROOT=str(root))
    procs = []
    procs.append(subprocess.Popen([str(api_exe)], env=env, cwd=str(root)))
    if bridge_exe.exists():
        procs.append(subprocess.Popen([str(bridge_exe)], env=env, cwd=str(root)))

    if not wait_http(f"http://127.0.0.1:{api_port}/health", 60):
        print("API did not start — closing")
        for p in procs:
            p.terminate()
        return 1

    url = f"http://127.0.0.1:{api_port}/"
    chrome = shutil.which("chrome") or shutil.which("google-chrome") \
        or shutil.which("msedge") or shutil.which("chromium")
    try:
        if chrome:
            subprocess.Popen([chrome, "--kiosk", url])
        else:
            import webbrowser
            webbrowser.open(url)
        print(f"opened kiosk {url} — press Ctrl+Shift+K in browser to exit kiosk")
    except Exception as e:
        print(f"open browser manually: {url} ({e})")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
