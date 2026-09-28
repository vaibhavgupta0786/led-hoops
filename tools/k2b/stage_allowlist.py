"""Stage operator folder: allowlist only, deny *.py / src / secrets."""
import argparse
import shutil
from pathlib import Path

ALLOW_DIRS = ["games/source", "games/source_group", "games/setting", "games/audio"]
DENY_NAMES = {".git", "__pycache__", "node_modules", "frontend/src", "tests"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", default=".")
    a = ap.parse_args()
    repo, dist, out = Path(a.repo).resolve(), Path(a.dist).resolve(), Path(a.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / "ui").mkdir(parents=True)
    for f in dist.rglob("*"):
        rel = f.relative_to(dist)
        tgt = out / "ui" / rel
        if f.is_dir():
            tgt.mkdir(parents=True, exist_ok=True)
        else:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, tgt)
    for d in ALLOW_DIRS:
        src = repo / d
        if src.exists():
            tgt = out / d
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, tgt, dirs_exist_ok=True)
    # bridge static assets (per-product path)
    for cand in [repo / "simulator" / "static", repo / "games" / "simulator" / "static"]:
        if cand.exists():
            shutil.copytree(cand, out / "simulator" / "static", dirs_exist_ok=True)
    (out / "data").mkdir(exist_ok=True)
    (out / "README_OPERATOR.txt").write_text(
        "LED Hoops operator build.\n"
        "Double-click supervisor/supervisor, Chrome opens full-screen.\n"
        "Exit kiosk: Ctrl+Shift+K.\n"
        "Update = replace this whole folder with the new release zip.\n"
    )
    bad = [p for p in out.rglob("*.py")] + [p for p in out.rglob("*.ts")] \
        + [p for p in out.rglob("*.tsx") if "ui/assets" not in p.as_posix()]
    # ui/assets/*.js is minified build output (allowed); raw src is never staged.
    bad = [p for p in bad if "ui/assets" not in p.as_posix()]
    if bad:
        print("DENY CHECK FAILED:")
        for p in bad[:20]:
            print(" ", p.relative_to(out))
        raise SystemExit(1)
    # LFS pointer check (levels must be real data, not pointer text)
    for p in out.rglob("*.led*"):
        head = p.read_bytes()[:120] if p.stat().st_size < 500 else b""
        if head.startswith(b"version https://git-lfs"):
            print(f"LFS POINTER IN STAGE: {p.relative_to(out)}")
            raise SystemExit(1)
    print(f"stage ok: {out}")


if __name__ == "__main__":
    raise SystemExit(main())
