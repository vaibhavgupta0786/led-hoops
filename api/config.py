"""
API Configuration - Game and Database Settings
Freeze-safe: works from source checkout AND from Nuitka/PyInstaller exe.
"""
import os
import sys
from pathlib import Path


def get_install_root() -> Path:
    """Folder holding the running app.

    Source run -> repo root (parent of api/).
    Frozen run -> exe folder, or PyInstaller bundle folder.
    Env INSTALL_ROOT always wins (for CI smoke / custom layout).
    """
    env = os.getenv("INSTALL_ROOT")
    if env:
        return Path(env)
    cands = []
    if getattr(sys, "frozen", False):
        # --standalone keeps sys.executable pointing at this real binary, so
        # the install root is its folder or the parent (bins live in subfolders).
        exe = getattr(sys, "executable", "") or ""
        if exe:
            d = Path(exe).resolve().parent
            cands += [d, d.parent]
    cands.append(Path(__file__).resolve().parent.parent)
    for c in cands:
        if (c / "games").is_dir() or (c / "ui").is_dir():
            return c
    return cands[0]


_INSTALL_ROOT = get_install_root()

# GAMES_ROOT: path to the game source directory.
# Default = ../games/ relative to this repo.
# Override with GAMES_ROOT env var if running from a different location.
_REPO_ROOT = Path(__file__).resolve().parent.parent
GAMES_ROOT = Path(os.getenv("GAMES_ROOT", str(_REPO_ROOT / "games")))
# Under freeze, default to games/ next to the exe when env is unset.
if "GAMES_ROOT" not in os.environ and getattr(sys, "frozen", False):
    GAMES_ROOT = _INSTALL_ROOT / "games"

# ui/ = built website files (frontend/dist copied next to exe as ui/).
UI_DIR = Path(os.getenv("UI_DIR", str(_INSTALL_ROOT / "ui")))

# Game Configuration
GAME_NAME = "hoops"
GAME_DIR = GAMES_ROOT
GAME_SOURCE_DIR = GAME_DIR / "game_play"
GAME_LEVEL_DIR = GAME_DIR / "source"
# Group-mode playlist root (same dash-folder layout as 1P tiers under source/).
# Override with GAMES_GROUP_LEVEL_DIR if needed.
GAME_GROUP_LEVEL_DIR = Path(
    os.getenv("GAMES_GROUP_LEVEL_DIR", str(GAME_DIR / "source_group"))
)
GAME_SETTING_DIR = GAME_DIR / "setting"

# Add game source to path for imports
if str(GAME_SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(GAME_SOURCE_DIR))

if str(GAME_DIR) not in sys.path:
    sys.path.insert(0, str(GAME_DIR))

# Database Configuration
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root")
DB_NAME = os.getenv("DB_NAME", "ledplaydb")

# API Configuration
API_HOST = "0.0.0.0"
API_PORT = int(os.getenv("API_PORT", 8000))  # Hoops default; Climb=8001, Hex=8002
API_DEBUG = os.getenv("API_DEBUG", "False").lower() == "true"

# Game Configuration
MAX_CONCURRENT_GAMES = 10
GAME_TIMEOUT_SECONDS = 600  # 10 minutes
GAME_STATE_UPDATE_INTERVAL = 0.016  # ~60fps

# Session Configuration
SESSION_DURATION_SECONDS = 3600  # 60 minutes
