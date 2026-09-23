"""
Game Manager - Hoops
Manages running Play instances and game state for the Hoops LED hoop game.
Headless: no tkinter GUI. Input via API (simulated "shot made" on a hoop column).
"""
import uuid
import threading
import time
import asyncio
import os
import math
import json
import sys
import shelve as _shelve
from typing import Dict, Optional
from loguru import logger
from .config import (
    GAME_TIMEOUT_SECONDS,
    MAX_CONCURRENT_GAMES,
    GAMES_ROOT,
    GAME_GROUP_LEVEL_DIR,
)
from .level_scaling import LevelScalingError, scale_level_to_platform

# Single absolute spelling (repo root on sys.path via config.py / freeze entry).
# A second relative spelling of the same module trips frozen compilers.
from hardware_config import (
    HardwareConfigError,
    parse_dimension,
    validate_hardware_config,
    validate_platform_config,
)

# Hardware mode: set USE_SERIAL_HD=1 env var to drive physical LED floor via serial.
# Sim mode (default): browser canvas only. HW mode: serial + canvas simultaneously.
USE_SERIAL_HD = os.environ.get("USE_SERIAL_HD", "0") == "1"
if USE_SERIAL_HD:
    _games_dir = str(GAMES_ROOT)
    if _games_dir not in sys.path:
        sys.path.insert(0, _games_dir)

# Mock hardware/network dependencies before importing game_play
# These are not needed for headless game logic:
# - tkinter: GUI (game_running.py imports tkinter.messagebox)
# - encryption: hardware dongle check (yanqian.py checks connected pedrive at module level)
# - led.led_control: hardware LED driver
# - net: network communication
from unittest.mock import MagicMock

# Mock ALL external dependencies (hardware, GUI, media, etc)
# Standard approach: mock before any imports to prevent ModuleNotFoundError
mocks = {
    # GUI/Display
    'tkinter': MagicMock(),
    'tkinter.messagebox': MagicMock(),
    'tkinter.font': MagicMock(),
    'gui': MagicMock(),
    'gui.app_gui': MagicMock(),
    'gui.gui_debugging': MagicMock(),
    'gui.gui_setting': MagicMock(),
    'gui.language': MagicMock(),
    'gui2': MagicMock(),
    'gui2.gui_led_table_editor': MagicMock(),
    'gui2.gui_led_canvas2': MagicMock(),
    'gui2.gui_table_editor': MagicMock(),
    'gui2.ui_player_setting': MagicMock(),
    'gui2.ui_table': MagicMock(),
    'gui2.gui_util': MagicMock(),
    'ui_design': MagicMock(),
    # Hardware: only mock in sim mode; real modules used when USE_SERIAL_HD=True
    **({} if USE_SERIAL_HD else {
        'serial': MagicMock(),
        'serial.tools': MagicMock(),
        'serial.tools.list_ports': MagicMock(),
        'led': MagicMock(),
        'led.led_control': MagicMock(),
        'led.communication': MagicMock(),
        'led.position_convert': MagicMock(),
        'led.led_serial_thread': MagicMock(),
        'led.led_control_c': MagicMock(),
    }),
    'net': MagicMock(),
    'socket': MagicMock(),
    # Audio/Video
    'pygame': MagicMock(),
    'pygame.mixer': MagicMock(),
    'audio_play': MagicMock(),
    'audio_play.audio': MagicMock(),
    'moviepy': MagicMock(),
    'moviepy.editor': MagicMock(),
    'cv2': MagicMock(),
    # Input
    'pynput': MagicMock(),
    'pynput.keyboard': MagicMock(),
    'pynput.mouse': MagicMock(),
    # Encryption
    'encryption': MagicMock(),
    'encryption.yanqian': MagicMock(),
    'rsa': MagicMock(),
    'Crypto': MagicMock(),
    'Crypto.Hash': MagicMock(),
    'Crypto.Cipher': MagicMock(),
    'Crypto.PublicKey': MagicMock(),
    'Crypto.Signature': MagicMock(),
    # Database
    'mysql': MagicMock(),
    'mysql.connector': MagicMock(),
    # Image processing
    'numpy': MagicMock(),
    'PIL': MagicMock(),
    'PIL.Image': MagicMock(),
    'PIL.ImageTk': MagicMock(),
}

for mod_name, mock in mocks.items():
    sys.modules[mod_name] = mock

# Hardware state (populated once by _hw_init when USE_SERIAL_HD=True)
_HW_DEFAULT_ROWS = 1   # Hoops: 1-row hoop strip
_HW_DEFAULT_COLS = 6   # Hoops: 6 hoop columns
_hw_led_control = None
_hw_layout_type = 0
_HW_DRAW_INTERVAL = float(os.environ.get("HW_DRAW_INTERVAL", "0.045"))
_hw_serial_lock = threading.Lock()


def _normalize_rgb(cell):
    """Ensure [R,G,B] ints. Play.clear_led_table can leave nested tuples."""
    if isinstance(cell, (list, tuple)):
        if len(cell) >= 3 and isinstance(cell[0], (int, float)):
            return [int(cell[0]), int(cell[1]), int(cell[2])]
        if len(cell) == 1:
            return _normalize_rgb(cell[0])
    return [0, 0, 0]

def _hw_init():
    """Open serial COM ports and init tile layout mapping. Called once at game start."""
    global _hw_led_control, _hw_layout_type
    if _hw_led_control is not None:
        return _hw_led_control
    driver = None
    com_init_attempted = False
    try:
        import shelve as _s
        from led import led_control as _lc
        driver = _lc
        platform = load_real_settings()
        db = _s.open(str(GAMES_ROOT / 'setting' / 'led_parameter'), flag='r')
        try:
            list_com_info = db.get('list_com_info', [])
            layout_type = db.get('led_layout_type', 0)
        finally:
            db.close()
        validated = validate_hardware_config(
            platform["grid_rows"],
            platform["grid_cols"],
            platform["floor_layout_coors_no_use"],
            layout_type,
            list_com_info,
        )
        rows = validated["rows"]
        cols = validated["cols"]
        no_use = validated["no_use"]
        layout_type = validated["layout_type"]
        list_com_info = validated["com_info"]
        _lc.init_layout(layout_type, rows, cols, no_use)
        com_init_attempted = True
        errors = _lc.init_com(list_com_info)
        if errors:
            raise RuntimeError(f"COM initialization errors: {errors}")
        if not getattr(_lc, "g_has_open", False):
            raise RuntimeError("serial driver did not open any COM port")
        logger.info(
            f"Hardware ready: {len(list_com_info)} port(s), "
            f"{rows}×{cols}, layout={layout_type}"
        )
        _hw_led_control = _lc
        _hw_layout_type = layout_type
    except Exception as e:
        if driver is not None and com_init_attempted:
            try:
                driver.close_com()
            except Exception as close_error:
                logger.warning(
                    f"Hardware init cleanup failed: {close_error}"
                )
        logger.error(f"Hardware init failed: {e}")
    return _hw_led_control


def _hw_blank_floor(led_table):
    """Send one all-black frame to the physical floor. Call on every game
    end/stop path (session-end, stop_game, clear_all) so the hardware
    doesn't stay stuck showing the last drawn frame after gameplay stops
    (see docs/TODO_HARDWARE_BLANK_ON_STOP.md). No-op in sim mode or if
    hardware/led_table were never initialized. Uses the SAME draw call as
    the per-frame HW block, just with an all-zero grid."""
    if not (USE_SERIAL_HD and _hw_led_control is not None):
        return
    if led_table is None:
        return
    try:
        rows = led_table.led_row
        cols = led_table.led_col
        blank = [[[0, 0, 0] for _ in range(cols)] for _ in range(rows)]
        with _hw_serial_lock:
            _hw_led_control.draw_screen_by_com(_hw_layout_type, blank)
    except Exception as e:
        logger.warning(f"HW blank-on-stop failed: {e}")

# Will import after config is set
# from game_play.Play import Play

# Hoops scoreable colors (PLUS_ARR from model/setting.py).
# Each lit hoop backboard column matching one of these → +1 on trigger.
# 2P (.ledb): P1=blue (0,0,254), P2=orange (254,128,0).
_HOOPS_COLOR_ARR = [
    (254, 128, 0),   # orange  - primary 1P target color
    (0, 0, 254),     # blue    - P1 in 2P levels
    (254, 254, 0),   # yellow
    (0, 254, 254),   # cyan
    (254, 0, 254),   # magenta
    (254, 254, 254), # white
]
_HOOPS_HAZARD_COLORS = {(254, 0, 0), (240, 0, 0)}  # RED variants

# Pause after clearing a scoreable wave before skipping to the next one.
WAVE_SKIP_DELAY_SEC = 1.0

# Settings from Hoops led_parameter shelve.
_LED_PARAM  = str(GAMES_ROOT / "setting" / "led_parameter")
_DEBUG_PARAM = str(GAMES_ROOT / "setting" / "debug_parameter")

# Sensible fallbacks if the shelve can't be read.
_SETTINGS_DEFAULTS = {
    "game_time_sec": 300.0,    # game_time_sw (min) * 60
    "life_value": 20,          # life_value_sw
    "leval_span": 0.8,         # leval_span_sw
    "tread_red_time": 0.01,
    "life_value_count_time": 1.2,
    "grid_rows": 1,            # value_high  — Hoops is 1 row (hoop strip)
    "grid_cols": 6,            # value_width — up to 6 hoop columns
    "floor_layout_coors_no_use": [],
    "blue_hide_max_time": 20.0,  # seconds before covered targets disappear
    "scode_divide_person": False,
    "scode_divide_time": False,
    "player_num": 2,
}

_settings_cache = None


def load_real_settings() -> dict:
    """Read game settings from the decompiled project's shelve DBs once.
    Returns a parsed dict; falls back to defaults on any error."""
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    s = dict(_SETTINGS_DEFAULTS)
    # led_parameter: game length, HP, speed span
    try:
        db = _shelve.open(_LED_PARAM, flag="r")
        try:
            gt = db.get("game_time_sw")
            if gt is not None:
                s["game_time_sec"] = float(gt) * 60.0   # stored in minutes
            lv = db.get("life_value_sw")
            if lv is not None:
                s["life_value"] = int(float(lv))
            ls = db.get("leval_span_sw")
            if ls is not None:
                s["leval_span"] = float(ls)
            # Parse integer-valued platform dims when valid; otherwise keep the
            # raw present value so callers can fail closed instead of truncating
            # fractions/bools into a fake 1x6 platform.
            if "value_high" in db:
                raw_rows = db.get("value_high")
                try:
                    s["grid_rows"] = parse_dimension(raw_rows, "grid_rows")
                except HardwareConfigError:
                    s["grid_rows"] = raw_rows
            if "value_width" in db:
                raw_cols = db.get("value_width")
                try:
                    s["grid_cols"] = parse_dimension(raw_cols, "grid_cols")
                except HardwareConfigError:
                    s["grid_cols"] = raw_cols
            if "floor_layout_coors_no_use" in db:
                raw_no_use = db.get("floor_layout_coors_no_use")
                if isinstance(raw_no_use, (list, tuple)):
                    s["floor_layout_coors_no_use"] = list(raw_no_use)
                else:
                    s["floor_layout_coors_no_use"] = raw_no_use
            dp = db.get("game_scode_divide_person")
            if dp is not None:
                s["scode_divide_person"] = bool(dp)
            dt = db.get("game_scode_divide_time")
            if dt is not None:
                s["scode_divide_time"] = bool(dt)
            pn = db.get("player_num_sw")
            if pn is not None:
                s["player_num"] = int(float(pn))
            bh = db.get("blue_hide_max_time_sw")
            if bh is not None:
                s["blue_hide_max_time"] = float(bh)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Could not read led_parameter: {e}; using defaults")
    # debug_parameter: red-penalty timing
    try:
        db = _shelve.open(_DEBUG_PARAM, flag="r")
        try:
            trt = db.get("tread_red_time")
            if trt is not None:
                s["tread_red_time"] = float(trt)
            lct = db.get("life_value_count_time")
            if lct is not None:
                s["life_value_count_time"] = float(lct)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Could not read debug_parameter: {e}; using defaults")
    _settings_cache = s
    logger.info(f"Loaded real settings: {s}")
    return s


# Level-progression tiers (dirs under source/, easy→hard). Category is locked
# by the START level's file type: .led = 1-player, .ledb = 2-player. A 1P
# session marathons only the 1P tiers and never crosses into 2P (and vice versa).
_TIERS_1P = ["-", "--"]      # casual → level (.led)
_TIERS_2P = ["---"]          # DK 2P (.ledb)
# Group mode: same dash-folder names as 1P, rooted at games/source_group/
_TIERS_GROUP = list(_TIERS_1P)


def _level_sort_key(p):
    stem = os.path.basename(p).rsplit(".", 1)[0]
    return (0, int(stem)) if stem.isdigit() else (1, stem.lower())


def _build_level_sequence(start_level):
    """Ordered list of level FILE PATHS forming the marathon.

    Category is locked by the START level's file type. A start level found in a
    1P (.led) tier yields a 1P-only chain (its tier onward through _TIERS_1P);
    a start level in a 2P (.ledb) tier yields a 2P-only chain. Never mixes.
    """
    import glob as _glob
    src = str(GAMES_ROOT)
    sl = str(start_level or "").strip()

    def _chain(dirs, ext):
        return [(d, sorted(_glob.glob(os.path.join(src, "source", d, f"*.{ext}")),
                           key=_level_sort_key)) for d in dirs]

    chain_1p = _chain(_TIERS_1P, "led")
    chain_2p = _chain(_TIERS_2P, "ledb")

    def _find(chain):
        for ti, (_d, files) in enumerate(chain):
            for fi, f in enumerate(files):
                stem = os.path.basename(f).rsplit(".", 1)[0]
                if stem == sl or stem.startswith(sl):
                    return ti, fi
        return None

    loc = _find(chain_1p)
    chain = chain_1p
    if loc is None:
        loc = _find(chain_2p)
        chain = chain_2p
    if loc is None:                       # unknown start → begin at 1P tier 0
        chain, loc = chain_1p, (0, 0)
        if not chain or not chain[0][1]:
            return []

    ti, fi = loc
    seq = list(chain[ti][1][fi:])         # remaining levels in the start tier
    for j in range(ti + 1, len(chain)):   # then all later SAME-category tiers
        seq.extend(chain[j][1])
    return seq


def _build_group_level_sequence(start_level=None):
    """Marathon paths from games/source_group/ (Group mode only, .led).

    Same progression rules as 1P tiers, but a separate admin-curated tree.
    If start_level is missing/unknown, begin at the first file of the first
    non-empty group tier.
    """
    import glob as _glob
    root = str(GAME_GROUP_LEVEL_DIR)
    sl = str(start_level or "").strip()
    if sl.lower() in ("", "auto", "none"):
        sl = ""

    chain = [
        (d, sorted(_glob.glob(os.path.join(root, d, "*.led")), key=_level_sort_key))
        for d in _TIERS_GROUP
    ]
    # Drop empty tiers so admins can leave a middle folder empty
    chain = [(d, files) for d, files in chain if files]
    if not chain:
        return []

    loc = None
    if sl:
        for ti, (_d, files) in enumerate(chain):
            for fi, f in enumerate(files):
                stem = os.path.basename(f).rsplit(".", 1)[0]
                if stem == sl or stem.startswith(sl):
                    loc = (ti, fi)
                    break
            if loc is not None:
                break
    if loc is None:
        loc = (0, 0)

    ti, fi = loc
    seq = list(chain[ti][1][fi:])
    for j in range(ti + 1, len(chain)):
        seq.extend(chain[j][1])
    return seq


def _level_uses_2p_scoring(lvl_path: str, session_player_count: int) -> bool:
    """Split P1/P2 scoring only for .ledb when the session is 2-player.

    Original Hoops: EditorGame (1P .led) scores every PLUS_ARR color to one
    counter; EditorGame2 (.ledb) maps blue→P1 and orange→P2. Do NOT infer
    multiplayer from blue+orange coexisting in a .led file."""
    return session_player_count >= 2 and str(lvl_path).lower().endswith(".ledb")


def _load_level_file(path):
    """Load one .led/.ledb: unzip, find the main gameplay shelve (the one with
    play_order=False; audio/anim dirs have play_order=True), return
    (dict_group, game_obj) as in-memory objects. (None, None) on failure."""
    import zipfile, tempfile, shelve
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(path, 'r') as z:
                z.extractall(tmpdir)
            best_go = best_dg = None
            for root, _, files in os.walk(tmpdir):
                if not any(f.startswith("game_file") for f in files):
                    continue
                gf = os.path.join(root, "game_file")
                if not os.path.exists(gf + ".dat"):
                    continue
                try:
                    db = shelve.open(gf)
                    go = db.get("para_key_game")
                    dg = db.get("dict_group")
                    db.close()
                    if go is None or dg is None:
                        continue
                    if not getattr(go, "play_order", True):
                        return dg, go          # main gameplay — done
                    elif best_go is None:
                        best_go, best_dg = go, dg
                except Exception:
                    continue
            return best_dg, best_go
    except Exception as e:
        logger.warning(f"Could not load level file {path}: {e}")
        return None, None


def _effects_dir() -> str:
    return os.environ.get(
        "HOOPS_EFFECTS_DIR",
        os.path.join(str(GAMES_ROOT), "source", "effects"),
    )


def _effect_path(name: str) -> str:
    return os.path.join(_effects_dir(), f"{name}.led")


def _load_effect_file(path):
    """Load one effect .led — prefers play_order=True shelves."""
    import zipfile
    import tempfile
    import shelve

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(path, "r") as zf:
                zf.extractall(tmpdir)
            fallback = (None, None)
            for root, _, files in os.walk(tmpdir):
                if not any(f.startswith("game_file") for f in files):
                    continue
                gf = os.path.join(root, "game_file")
                if not os.path.exists(gf + ".dat"):
                    continue
                try:
                    db = shelve.open(gf)
                    go = db.get("para_key_game")
                    dg = db.get("dict_group")
                    db.close()
                    if go is None or dg is None:
                        continue
                    if getattr(go, "play_order", False):
                        return dg, go
                    if fallback[0] is None:
                        fallback = (dg, go)
                except Exception:
                    continue
            return fallback
    except Exception as exc:
        logger.warning(f"Could not load effect file {path}: {exc}")
        return None, None


def _effect_board_time(dict_group) -> float:
    return max(
        (float(getattr(g, "end_time_sec", 0) or 0) for g in dict_group.values()),
        default=0.3,
    )


def _countdown_step_from_pass(dict_group, total_pass):
    """Map active hoop count in countdown.led to 3/2/1/go."""
    active_cols = set()
    for group in dict_group.values():
        if not (group.start_time_sec <= total_pass <= group.end_time_sec):
            continue
        for cell in getattr(group, "start_member", []) or []:
            active_cols.add(cell[1])
    n = len(active_cols)
    board_time = _effect_board_time(dict_group)
    if n <= 2:
        return 3
    if n <= 4:
        return 2
    if total_pass < board_time - 0.02:
        return 1
    return "go"


def _backend_audio_active(audio_mgr) -> bool:
    """True only when backend AudioManager is enabled (not sim-disabled)."""
    return bool(audio_mgr and getattr(audio_mgr, "_enabled", False))


def _build_effect_led_display(dict_group, total_pass, led_table):
    try:
        from model.setting import Setting

        floor_type = Setting.FLOOR_LIGHT
    except ImportError:
        floor_type = "floor_light"
    cell_win = {}
    for group in dict_group.values():
        if getattr(group, "type", None) != floor_type:
            continue
        if not (group.start_time_sec <= total_pass <= group.end_time_sec):
            continue
        main_color = _group_main_color(group.color)
        for cell in getattr(group, "start_member", []) or []:
            cell_win[cell] = (group, "effect", main_color)
    return _build_hoops_led_display(cell_win, led_table, total_pass, {})


def _play_led_panel(
    game,
    play,
    led_table,
    path,
    *,
    phase,
    settings,
    audio_mgr=None,
    stinger=None,
    countdown_ticks=False,
):
    """Run one transition .led panel on the marathon thread."""
    game.begin_level_transition()
    initial_step = 3 if countdown_ticks else None
    game._last_countdown_step = initial_step
    game.update_state(
        phase=phase,
        accepting_input=False,
        countdown_step=initial_step,
        backend_audio=_backend_audio_active(audio_mgr),
    )
    if play is None:
        game.finish_level_transition()
        return

    dict_group, game_obj = _load_effect_file(path)
    if not dict_group or game_obj is None:
        logger.warning(f"Effect missing or corrupt: {path}")
        game.finish_level_transition()
        return

    try:
        dict_group, game_obj = prepare_level_for_platform(
            dict_group, game_obj, settings
        )
    except Exception as exc:
        logger.warning(f"Effect prepare failed for {path}: {exc}")
        game.finish_level_transition()
        return

    board_time = _effect_board_time(dict_group)
    if audio_mgr:
        audio_mgr.stop_bgm()
        if stinger:
            audio_mgr.play_sfx(stinger)

    def _effect_frame(play_self, dgroup, time_pass, total_pass):
        if not game.running:
            return False
        session_elapsed = time.time() - game.session_start
        led_display = _build_effect_led_display(dgroup, total_pass, led_table)
        countdown_step = None
        if countdown_ticks:
            if game._last_countdown_step == "go":
                countdown_step = "go"
            else:
                countdown_step = _countdown_step_from_pass(dgroup, total_pass)
                if countdown_step != game._last_countdown_step:
                    game._last_countdown_step = countdown_step
                    if audio_mgr and countdown_step in (3, 2, 1):
                        audio_mgr.play_sfx(audio_mgr.tick_sfx)
        game.update_state(
            phase=phase,
            led_display=led_display,
            time_elapsed=session_elapsed,
            time_left=max(0, game.game_time_sec - session_elapsed),
            accepting_input=False,
            countdown_step=countdown_step,
            backend_audio=_backend_audio_active(audio_mgr),
        )
        draw_now = time.time()
        if USE_SERIAL_HD and _hw_led_control is not None and draw_now - getattr(
            game, "_hw_last_draw", 0
        ) >= _HW_DRAW_INTERVAL:
            with _hw_serial_lock:
                try:
                    _write_hoops_hardware_frame(
                        game,
                        _hw_led_control,
                        _hw_layout_type,
                        led_table,
                        led_display,
                        draw_time=draw_now,
                    )
                except Exception as hw_err:
                    logger.warning(f"Effect HW draw failed: {hw_err}")
        time.sleep(0.01)
        return total_pass < board_time

    prev_cb = play.callback
    play.callback = _effect_frame
    play.running_state = True
    play.total_pass = 0
    if getattr(game_obj, "play_order", True):
        play.running(dict_group)
    else:
        play.running_by_blue(dict_group)
    play.callback = prev_cb
    game._last_countdown_step = None
    game.finish_level_transition()


def _finish_session(
    game,
    play,
    led_table,
    audio_mgr,
    *,
    reason,
    settings,
    play_clear=True,
):
    """Session end: optional clear hold → stinger → black. No countdown."""
    if play_clear and reason in ("timeout", "out_of_life", "stopped"):
        _play_led_panel(
            game,
            play,
            led_table,
            _effect_path("level_clear"),
            phase="session_end",
            settings=settings,
            audio_mgr=audio_mgr,
            stinger=getattr(audio_mgr, "transition_stinger", None)
            if audio_mgr
            else None,
        )
    game.update_state(
        phase="session_end",
        accepting_input=False,
        countdown_step=None,
        led_display=[[0, 0, 0] for _ in range(led_table.led_row * led_table.led_col)]
        if led_table
        else [],
    )
    if not getattr(game, "_floor_blanked", False):
        _hw_blank_floor(getattr(game, "led_table", None) or led_table)
        game._floor_blanked = True
    game._session_over = True
    game._end_reason = reason
    if reason == "timeout":
        game.update_state(game_over_reason="timeout", result=2)
    elif reason == "out_of_life":
        game.update_state(game_over_reason="out_of_life", result=0)
    elif reason == "stopped":
        game.update_state(game_over_reason="stopped", result=0)


def prepare_level_for_platform(dict_group, game_obj, settings):
    """Scale one freshly loaded level to the configured physical platform.

    The scaler mutates in place; this boundary preserves and returns the input
    object identities in the loader's ``(dict_group, game_obj)`` order.
    """
    try:
        rows, cols, no_use = validate_platform_config(
            settings.get("grid_rows"),
            settings.get("grid_cols"),
            settings.get("floor_layout_coors_no_use", ()),
        )
    except HardwareConfigError as error:
        raise ValueError(str(error)) from error
    scale_level_to_platform(game_obj, dict_group, rows, cols, no_use)
    return dict_group, game_obj


def _set_input_acceptance(game, enabled, *, input_lock_held=False):
    """Change input availability and invalidate requests crossing the boundary."""
    def _set():
        game.accepting_input = enabled
        game._input_epoch = getattr(game, "_input_epoch", 0) + 1

    lock = getattr(game, "input_lock", None)
    if lock is not None and not input_lock_held:
        with lock:
            _set()
    else:
        _set()


def _mark_session_error(game, reason, *, input_lock_held=False):
    """Record a stable non-successful session outcome and disable input."""
    _set_input_acceptance(game, False, input_lock_held=input_lock_held)
    game._session_over = True
    game._end_reason = reason
    game.update_state(game_over_reason=reason, result=0)


def _mark_level_error(game, *, input_lock_held=False):
    """Record the common level failure outcome."""
    _mark_session_error(
        game, "level_error", input_lock_held=input_lock_held
    )


def _resolve_session_outcome(game):
    """Return the final result and reason without inferring errors as success."""
    state = game.get_state()
    state_result = state.get("result")
    state_reason = state.get("game_over_reason")
    if state_result is not None:
        return state_result, state_reason or game._end_reason or "session_end"
    if game._end_reason == "timeout":
        return 2, state_reason or "timeout"
    if game._end_reason:
        return 0, state_reason or game._end_reason
    return 1, state_reason or "session_end"


def _handle_frame_callback_error(game, game_id, error):
    """Log a frame failure, mark the session failed, and stop Play."""
    logger.error(f"Frame callback error {game_id}: {error}")
    _mark_level_error(game)
    return False


def _handle_stopped_session(game):
    """Record an explicit non-success when the session is externally stopped."""
    _mark_session_error(game, "stopped")
    return False


def _run_level_attempt(
    level_path,
    game,
    settings,
    setup_level,
    play,
    *,
    loader=None,
):
    """Load, reset, prepare, set up, and run one fresh level attempt."""
    if loader is None:
        loader = _load_level_file
    with game.input_lock:
        _set_input_acceptance(game, False, input_lock_held=True)
        game.dict_group = None
        game.zone = None
        stage = "load"
        try:
            dict_group, game_obj = loader(level_path)
            if not dict_group or game_obj is None:
                logger.error(
                    f"Level error for {level_path}: level failed to load; "
                    "aborting session"
                )
                _mark_level_error(game, input_lock_held=True)
                return None

            stage = "reset"
            game.reset_for_level()
            stage = "prepare"
            try:
                prepared = prepare_level_for_platform(dict_group, game_obj, settings)
            except Exception as exc:
                logger.error(
                    f"Level error for {level_path}: preparation failed: {exc}; "
                    "aborting session"
                )
                _mark_level_error(game, input_lock_held=True)
                return None

            stage = "setup"
            setup_level(prepared[0], prepared[1], level_path)
            led_table = getattr(game, "led_table", None)
            if led_table is not None:
                led_table.clear_input_state()
            play.running_state = True
            play.total_pass = 0
            logger.info(
                f"▶ Level {os.path.basename(level_path).rsplit('.', 1)[0]}: "
                f"groups={len(prepared[0])}, "
                f"mp={getattr(game, 'multiplayer', False)}, "
                f"board_time={getattr(game, 'board_time_sec', 0)}s, "
                f"score={getattr(game, 'score', 0)}, "
                f"life={getattr(game, 'life', 0)}"
            )
            _set_input_acceptance(game, True, input_lock_held=True)
            if hasattr(game, "update_state"):
                game.update_state(
                    phase="playing",
                    accepting_input=True,
                    countdown_step=None,
                )
        except Exception as exc:
            logger.error(
                f"Level error for {level_path}: {stage} failed: {exc}; "
                "aborting session"
            )
            _mark_level_error(game, input_lock_held=True)
            raise

    try:
        if game._play_order:
            play.running(prepared[0])
        else:
            play.running_by_blue(prepared[0])
    except Exception as exc:
        logger.error(
            f"Level error for {level_path}: setup or Play failed: {exc}; "
            "aborting session"
        )
        _mark_level_error(game)
        raise
    _set_input_acceptance(game, False)
    return prepared


class HeadlessLedTable:
    """In-memory LED table — replaces tkinter LedTable for headless API operation.
    Same interface as gui2/gui_led_table_editor.LedTable but zero GUI deps.
    Ported from LED-Hex SimulatorLedTable.
    """

    def __init__(self, wall_light_arr_len: int, led_row: int, led_col: int):
        self.led_row = led_row
        self.led_col = led_col
        self.row = led_row
        self.col = led_col
        # Floor LED colors: led_table[row][col] = [R, G, B]
        self.led_table = [[[0, 0, 0] for _ in range(led_col)] for _ in range(led_row)]
        # Tile press state: True = being stepped on
        self._state_table = [[False] * led_col for _ in range(led_row)]
        self.table_state = self._state_table   # shared ref
        self.state_table = self._state_table   # alias (game_manager callback uses this)
        self.state_2array = [[5] * led_col for _ in range(led_row)]
        self.g_wall_has_been_tread_arr2 = [[False] * led_col for _ in range(led_row)]
        # Wall arrays
        self._wall_light_arr = [[0, 0, 0] for _ in range(wall_light_arr_len)]
        self._wall_light_state_array = [False] * wall_light_arr_len
        self._wall_screen_arr = [0] * wall_light_arr_len
        # Per-tile scoring state
        self.red_table = [[False] * led_col for _ in range(led_row)]
        self.green_table = [[False] * led_col for _ in range(led_row)]
        self.safe_table = [[False] * led_col for _ in range(led_row)]
        self.deduct_table = [[False] * led_col for _ in range(led_row)]
        self.plus_table = [[None] * led_col for _ in range(led_row)]
        self.other_color_table = [[False] * led_col for _ in range(led_row)]
        self.blue_table = [[False] * led_col for _ in range(led_row)]
        self.tread_short_stay = [[None] * led_col for _ in range(led_row)]
        self.goal_color = None
        self.goal_color2 = None
        self.safe_color = None
        self.canvas = None
        self.led_coors_click = [[0, 0], False]
        self.led_coors_click_wall = [[0, 0], False]

    def resize(self, led_row: int, led_col: int) -> None:
        """Resize grid to match per-level dimensions (Hoops levels are often 1×5)."""
        if led_row == self.led_row and led_col == self.led_col:
            return
        old_state = self._state_table
        self.led_row = self.row = led_row
        self.led_col = self.col = led_col
        self.led_table = [[[0, 0, 0] for _ in range(led_col)] for _ in range(led_row)]
        self._state_table = [[False] * led_col for _ in range(led_row)]
        self.table_state = self._state_table
        self.state_table = self._state_table
        self.state_2array = [[5] * led_col for _ in range(led_row)]
        self.g_wall_has_been_tread_arr2 = [[False] * led_col for _ in range(led_row)]
        self.red_table = [[False] * led_col for _ in range(led_row)]
        self.green_table = [[False] * led_col for _ in range(led_row)]
        self.safe_table = [[False] * led_col for _ in range(led_row)]
        self.deduct_table = [[False] * led_col for _ in range(led_row)]
        self.plus_table = [[None] * led_col for _ in range(led_row)]
        self.other_color_table = [[False] * led_col for _ in range(led_row)]
        self.blue_table = [[False] * led_col for _ in range(led_row)]
        self.tread_short_stay = [[None] * led_col for _ in range(led_row)]
        for r in range(min(led_row, len(old_state))):
            for c in range(min(led_col, len(old_state[0]))):
                self._state_table[r][c] = old_state[r][c]

    def clear_input_state(self) -> None:
        """Release every pressed cell while preserving state-table aliases."""
        for row in self._state_table:
            for col in range(len(row)):
                row[col] = False
        self.state_table = self._state_table
        self.table_state = self._state_table

    # ── State table ────────────────────────────────────────────────────
    def get_state_table(self):
        return self._state_table

    def get_state_2array(self):
        return self.state_2array

    def get_g_wall_has_been_tread_arr2(self):
        return self.g_wall_has_been_tread_arr2

    # ── Wall accessors ─────────────────────────────────────────────────
    def get_wall_light_arr(self):
        return self._wall_light_arr

    def get_wall_light_state_array(self):
        return self._wall_light_state_array

    def get_wall_screen_arr(self):
        return self._wall_screen_arr

    # ── Color output ───────────────────────────────────────────────────
    def set_color_table_by_set_cell(self, start_member, color) -> None:
        c = list(color) if isinstance(color, (tuple, list)) else [0, 0, 0]
        for cell in (start_member or []):
            try:
                r_idx, c_idx = int(round(cell[0])), int(round(cell[1]))
                if 0 <= r_idx < self.led_row and 0 <= c_idx < self.led_col:
                    self.led_table[r_idx][c_idx] = c[:]
            except (IndexError, TypeError, ValueError):
                pass

    def set_table_color(self, table, color=None):
        c = list(color) if isinstance(color, (tuple, list)) else [0, 0, 0]
        for row in table:
            for i in range(len(row)):
                row[i] = c[:]

    def redraw_led_table_default(self, line=0, draw_canvas=True):
        pass  # no-op: game_manager reads led_table directly

    def draw_led_color(self):
        pass

    def clear_led_table(self):
        for r in range(self.led_row):
            for c in range(self.led_col):
                self.led_table[r][c] = [0, 0, 0]
        for i in range(len(self._wall_light_arr)):
            self._wall_light_arr[i] = [0, 0, 0]
        self._wall_screen_arr = [0] * len(self._wall_light_arr)

    def screen_mouse_click_state_get(self):
        pass

    # ── Input (press/release from simulator) ──────────────────────────
    def press_cell(self, row: int, col: int):
        if 0 <= row < self.led_row and 0 <= col < self.led_col:
            self._state_table[row][col] = True

    def release_cell(self, row: int, col: int):
        if 0 <= row < self.led_row and 0 <= col < self.led_col:
            self._state_table[row][col] = False

    # ── tkinter-compat stubs ───────────────────────────────────────────
    def pack(self, **kw): pass
    def grid(self, **kw): pass
    def update(self): pass
    def update_idletasks(self): pass
    def configure(self, **kw): pass
    def config(self, **kw): pass
    def destroy(self): pass
    def bind(self, *a, **kw): pass
    def unbind(self, *a, **kw): pass
    def after(self, ms, func=None, *args):
        import threading
        if func:
            t = threading.Timer(ms / 1000.0, func, args)
            t.daemon = True
            t.start()
    def after_cancel(self, *a): pass
    def winfo_width(self): return self.led_col * 44
    def winfo_height(self): return self.led_row * 38
    def get_canvas_table_size(self): return (self.led_row, self.led_col)


def _normalize_rings(cell):
    """Normalize a led_table cell to 3 ring colors [[r,g,b],[r,g,b],[r,g,b]]
    (outer, mid, inner). Cell is normally a 3-ring list, but tolerate a flat
    (r,g,b) (broadcast to all rings)."""
    try:
        if isinstance(cell, (list, tuple)) and len(cell) > 0:
            if isinstance(cell[0], (list, tuple)):
                rings = [[int(c[0]), int(c[1]), int(c[2])] for c in cell[:3]]
                while len(rings) < 3:
                    rings.append(rings[-1])
                return rings
            # flat (r,g,b) -> all rings same
            rgb = [int(cell[0]), int(cell[1]), int(cell[2])]
            return [rgb, rgb, rgb]
    except Exception:
        pass
    return [[0, 0, 0], [0, 0, 0], [0, 0, 0]]


def _cell_is_lit(cell):
    """True if any ring of the cell has a non-zero channel."""
    for ring in _normalize_rings(cell):
        if ring[0] or ring[1] or ring[2]:
            return True
    return False


def _cell_is_red(cell):
    """True if any ring is RED-dominant ((254,0,0)-like). Red = penalty tile."""
    for r, g, b in _normalize_rings(cell):
        if r >= 200 and g < 80 and b < 80:
            return True
    return False


def _group_main_color(color):
    """A group's representative color = its middle ring (ring[1]); ring[0] is a
    constant green marker. Returns an (r,g,b) tuple."""
    rings = _normalize_rings(color)
    return tuple(rings[1])


def _rgb_is_deduct(rgb):
    """DEDUCT_COLOR (254,0,48): consume + penalty (distinct from plain red)."""
    return rgb[0] >= 200 and rgb[1] < 80 and 30 <= rgb[2] <= 90


def _rgb_is_red(rgb):
    """Plain RED (254,0,0): hazard, stays, repeats. Excludes DEDUCT (b~48)."""
    return rgb[0] >= 200 and rgb[1] < 80 and rgb[2] < 30


def _classify_hoops_frame(dgroup, total_pass, led_table, multiplayer):
    """Return the physical-cell winners and scoring sets for one frame."""
    try:
        from model.setting import Setting
    except ImportError:
        floor_light = "floor_light"
    else:
        floor_light = Setting.FLOOR_LIGHT

    p1_color = (0, 0, 254)
    p2_color = (254, 128, 0)
    green = (0, 254, 0)
    score_colors = set(_HOOPS_COLOR_ARR)
    cell_win = {}

    for group in dgroup.values():
        members = getattr(group, "start_member", None)
        if not members:
            continue
        if getattr(group, "type", None) != floor_light:
            continue
        if not (group.start_time_sec <= total_pass <= group.end_time_sec):
            continue
        main_color = _group_main_color(group.color)
        if main_color == green:
            rank, category = 3, "green"
        elif _rgb_is_deduct(main_color):
            rank, category = 2, "deduct"
        elif main_color in _HOOPS_HAZARD_COLORS:
            rank, category = 2, "red"
        elif multiplayer and main_color == p1_color:
            rank, category = 1, "p1"
        elif multiplayer and main_color == p2_color:
            rank, category = 1, "p2"
        elif not multiplayer and main_color in score_colors:
            rank, category = 1, "goal"
        else:
            rank, category = 0, "decor"
        for cell in members:
            row = round(cell[0])
            col = round(cell[1])
            if not (0 <= row < led_table.led_row and 0 <= col < led_table.led_col):
                continue
            previous = cell_win.get((row, col))
            if previous is None or rank > previous[0]:
                cell_win[(row, col)] = (rank, category, main_color)

    goal_cells = set()
    goal2_cells = set()
    red_cells = set()
    deduct_cells = set()
    green_cells = set()
    for cell, (_, category, _) in cell_win.items():
        if category == "green":
            green_cells.add(cell)
        elif category == "deduct":
            deduct_cells.add(cell)
        elif category == "red":
            red_cells.add(cell)
        elif category in ("p1", "goal"):
            goal_cells.add(cell)
        elif category == "p2":
            goal2_cells.add(cell)
    return (
        cell_win,
        goal_cells,
        goal2_cells,
        red_cells,
        deduct_cells,
        green_cells,
    )


def _score_pressed_hoops_cells(game, led_table):
    """Score the physical coordinates currently asserted by either input path."""
    state = led_table.state_table
    for row in range(led_table.led_row):
        for col in range(led_table.led_col):
            if state[row][col]:
                game.try_score_cell(row, col)


def _build_hoops_led_display(cell_win, led_table, total_pass, flashes, *, now=None):
    """Build the simulator's flat, row-major physical LED buffer."""
    cols = led_table.led_col
    rows = led_table.led_row
    breath = 0.55 + 0.45 * (
        0.5 + 0.5 * math.sin(total_pass * math.pi)
    )
    led_display = [[0, 0, 0] for _ in range(rows * cols)]
    for (row, col), (_, category, main_color) in cell_win.items():
        index = row * cols + col
        if category in ("goal", "p1", "p2"):
            led_display[index] = [int(channel * breath) for channel in main_color]
        else:
            led_display[index] = [
                int(main_color[0]),
                int(main_color[1]),
                int(main_color[2]),
            ]

    if now is None:
        now = time.time()
    for cell, started_at in list(flashes.items()):
        elapsed = now - started_at
        if elapsed > 0.5:
            flashes.pop(cell, None)
            continue
        row, col = cell
        on = int(elapsed / 0.2) % 2 == 0
        led_display[row * cols + col] = (
            [255, 0, 0] if on else [0, 0, 0]
        )
    return led_display


def _write_hoops_hardware_frame(
    game,
    driver,
    layout_type,
    led_table,
    led_display,
    *,
    draw_time,
):
    """Draw, record success, then update sensors in the original call order."""
    rows = led_table.led_row
    cols = led_table.led_col
    needed = rows * cols
    if len(led_display) < needed:
        led_display = led_display + [[0, 0, 0]] * (needed - len(led_display))
    hardware_display = [
        [
            _normalize_rgb(led_display[row * cols + col])
            for col in range(cols)
        ]
        for row in range(rows)
    ]
    driver.draw_screen_by_com(layout_type, hardware_display)
    game._hw_last_draw = draw_time
    game._hw_draw_count = getattr(game, "_hw_draw_count", 0) + 1
    driver.update_screen_state_by_com(
        layout_type,
        led_table.state_table,
        led_table.state_table,
    )


_GREEN_COLOR = (0, 254, 0)
_COVER_COLORS = _HOOPS_HAZARD_COLORS | {_GREEN_COLOR}


def _hoops_apply_cover_disappear(dgroup, total_pass, blue_hide_max_time):
    """Remove PLUS tiles hidden under red/green cover (Hoops disappear mode).

    Faithful to gui_editor_game.color_cover_over_times_disappear — after
    blue_hide_max_time seconds, scoreable cells overlapped by cover vanish."""
    try:
        from model.setting import Setting
    except ImportError:
        return

    PLUS = set(_HOOPS_COLOR_ARR)
    hide = float(blue_hide_max_time or 20.0)

    scoreable_at = set()
    for g in dgroup.values():
        if getattr(g, "type", None) != Setting.FLOOR_LIGHT:
            continue
        mc = _group_main_color(g.color)
        if mc not in PLUS:
            continue
        if not (g.start_time_sec <= total_pass <= g.end_time_sec):
            continue
        for cell in (g.start_member or []):
            scoreable_at.add((round(cell[0]), round(cell[1])))
    if not scoreable_at:
        return

    coors_list = set()
    coors_list_red = set()
    for g in dgroup.values():
        if getattr(g, "type", None) != Setting.FLOOR_LIGHT:
            continue
        sm = g.start_member
        if not sm:
            continue
        mc = _group_main_color(g.color)
        is_static_cover = (
            g.speed == 0
            and mc in _COVER_COLORS
            and g.start_time_sec + hide + 5 < total_pass < g.end_time_sec
        )
        for cell in sm:
            ci, cj = round(cell[0]), round(cell[1])
            if (ci, cj) not in scoreable_at:
                continue
            if is_static_cover:
                coors_list_red.add((ci, cj))
            else:
                coors_list.add((ci, cj))
    coors_all = coors_list | coors_list_red

    cover_now = set()
    for g in dgroup.values():
        if getattr(g, "type", None) != Setting.FLOOR_LIGHT:
            continue
        if not (g.start_time_sec <= total_pass <= g.end_time_sec):
            continue
        mc = _group_main_color(g.color)
        if mc in _HOOPS_HAZARD_COLORS or mc == _GREEN_COLOR:
            for cell in (g.start_member or []):
                cover_now.add((round(cell[0]), round(cell[1])))

    for g in dgroup.values():
        if getattr(g, "type", None) != Setting.FLOOR_LIGHT or g.speed != 0:
            continue
        mc = _group_main_color(g.color)
        if mc not in PLUS:
            continue
        if not (g.start_time_sec + hide < total_pass < g.end_time_sec):
            continue
        sm = g.start_member
        if not sm:
            continue
        for cell in list(sm):
            ci, cj = round(cell[0]), round(cell[1])
            if (ci, cj) in cover_now and (ci, cj) in coors_all:
                try:
                    if isinstance(sm, set):
                        sm.discard((ci, cj))
                    else:
                        sm.remove((ci, cj))
                except (KeyError, ValueError, TypeError):
                    pass


class HeadlessGameGUI:
    """Mock GUI parent for Play.running_new() - provides LED update callback"""

    def __init__(self, led_table):
        self.led_table = led_table

    def update_draw_led_table_idle_game(self, dict_group, total_pass=0, time_pass=0):
        """Called by Play.running_new() to update LED display"""
        try:
            if dict_group:
                for key, value in dict_group.items():
                    group = value
                    set_cell = group.start_member
                    start_time = group.start_time_sec
                    end_time = group.end_time_sec
                    if set_cell is not None and total_pass > start_time and total_pass < end_time:
                        color = getattr(group, 'color', (0, 255, 0))
                        if hasattr(self.led_table, 'set_color_table_by_set_cell'):
                            self.led_table.set_color_table_by_set_cell(set_cell, color)
        except Exception as e:
            logger.debug(f"LED update error: {e}")

    def clear_last_wall_display(self):
        """Clear display for next frame"""
        pass


class GameInstance:
    """Single running game instance"""

    def __init__(self, game_id: str, card_id: str, level: int, difficulty: str,
                 player_count: int = 1, mode: str = None):
        self.game_id = game_id
        self.card_id = card_id
        self.level = level
        self.difficulty = difficulty
        self.mode = (mode or "").strip().lower() or None  # "group" or None
        # Group mode is always 1P (.led under source_group/)
        if self.mode == "group":
            player_count = 1
        self.session_player_count = max(1, min(2, int(player_count or 1)))
        self.created_at = time.time()
        self.play = None  # Play object
        self._level_time_pass = 0.0  # per-level timeline (Play.total_pass)
        self.led_table = None  # LedTable instance (for press input)
        self.dict_group = None  # level groups (for consume-on-hit)
        self.flashes = {}  # cell -> wall-clock start time (display-only hit flash)
        self.score = 0  # accumulated score from presses on lit tiles
        self.scored_active = set()  # goal cells already scored this appearance
        # Per-frame cell classification (rebuilt each frame from dict_group):
        self.goal_cells = set()    # floor cells matching P1 goal color (scoreable)
        self.goal2_cells = set()   # floor cells matching P2 goal color (2-player)
        self.red_cells = set()     # in-time red hazard cells (penalty, stays)
        self.deduct_cells = set()  # DEDUCT_COLOR cells (penalty + consume)
        self.goal_color = None     # P1 goal color (from goal_led indicator)
        self.goal2_color = None    # P2 goal color (from goal2_led indicator)
        self.score2 = 0            # P2 score (0 in single-player)
        self.scored_active2 = set()# P2 scored cells this appearance
        self.multiplayer = False   # True when level has goal2_led
        self.zone = None          # (row_from,row_to,col_from,col_to) active area
        # 2P respawn: consumed goal tiles reappear after delay (only for .ledb multiplayer)
        self.pending_respawn = []  # [[group, (i,j), reappear_wall_time], ...]
        self.respawn_delay = 8.0   # seconds; tunable
        self.input_lock = threading.Lock()  # guards state_table writes
        self.accepting_input = False
        self._input_epoch = 0
        self.running = False

        # Real settings (game length + HP). Loaded from led_parameter.
        _s = load_real_settings()
        self.game_time_sec = _s["game_time_sec"]   # session limit (300s)

        # Runtime override pushed from the central RFID server (Settings page):
        # default_difficulty/session_minutes. Applied after the shelve-derived
        # defaults above but only takes effect for difficulty when the caller
        # didn't already pass one explicitly (StartGameRequest.difficulty is
        # required today, so this is a no-op until a caller omits it).
        _override_path = GAMES_ROOT / "setting" / "runtime_overrides.json"
        if _override_path.exists():
            try:
                with open(_override_path) as _f:
                    _overrides = json.load(_f)
                if _overrides.get("session_minutes"):
                    self.game_time_sec = float(_overrides["session_minutes"]) * 60.0
                if _overrides.get("default_difficulty") and not getattr(self, "difficulty", None):
                    self.difficulty = _overrides["default_difficulty"]
            except Exception as _e:
                logger.warning(f"Could not read runtime overrides: {_e}")

        self.board_time_sec = 1e9                   # board length (max group end); set on load
        self.result = None                          # 0 lose / 1 complete / 2 timeout
        self.max_life = _s["life_value"]           # 20 HP
        self.life = self.max_life
        # Final-score normalization (game_scode_rule): divide raw score by
        # player count and/or game-time (minutes). Applied at session end only;
        # live `score` stays raw for display.
        self._scode_divide_person = _s.get("scode_divide_person", True)
        self._scode_divide_time = _s.get("scode_divide_time", True)
        # Default 1 player; _setup_level bumps to 2 for actual 2P (DK) levels.
        # (player_num_sw is the machine's max-player config, not per-game.)
        self._player_num = 1
        self.last_life_loss_time = 0.0             # legacy global gate
        self._cell_red_penalty_at = {}             # per-cell red penalty timing
        self._life_count_time = _s["life_value_count_time"]
        self._blue_hide_max_time = _s.get("blue_hide_max_time", 20.0)
        self._cover_disappear = True   # per-level; set from game obj in _setup_level
        self._play_order = False       # False → running_by_blue (all Hoops .led levels)
        self._wave_skip_at = None        # total_pass when wave skip is allowed
        self.green_cells = set()                   # shield from red (updated each frame)

        # ── SESSION (5-min marathon) state ──────────────────────────────
        # Score + lives persist across levels; session ends on life<=0 or
        # timer<=0. Player picks a starting level; we marathon to series end.
        self.session_start = None      # wall-clock when first level begins
        self.level_sequence = []       # ordered list of level FILE PATHS
        self.current_level_id = None   # e.g. "A005" (for frontend display)
        self.levels_cleared = 0        # how many levels finished this session
        self._session_over = False     # True -> stop the session loop
        self._end_reason = None        # why the marathon loop exited (timeout/etc)
        self._level_cleared = False    # True -> advance to next level
        self._restart_level = False    # True -> replay same level (life=0, time left)
        self._saw_scoreable = False    # True once any scoreable wave appeared this level
        self._floor_blanked = False
        self._audio_mgr = None
        self._last_countdown_step = None

        self.current_state = {
            "score": 0,
            "time_elapsed": 0.0,
            "time_left": self.game_time_sec,
            "life": self.max_life,
            "max_life": self.max_life,
            "display_lives": math.ceil(self.max_life / 4),   # 20 HP -> 5 hearts
            "display_max": math.ceil(self.max_life / 4),     # 5
            "score2": 0,
            "multiplayer": False,
            "player_pos": [0, 0],
            "led_display": [],
            "game_over": False,
            "game_over_reason": "",
            "result": None,
            "current_level": None,
            "levels_cleared": 0,
            "started_at": "",
            "phase": "idle",
            "countdown_step": None,
            "accepting_input": False,
            "backend_audio": False,
        }
        self.thread = None

    def compute_final_score(self, raw_score):
        """Leaderboard score normalization (faithful to game_scode_rule):
          final = raw / player_count (if divide_person) / minutes (if divide_time)
        game_time is in MINUTES (game_time_sw). Live display uses raw score;
        this is only for the saved/leaderboard result."""
        scode = float(raw_score)
        if self._scode_divide_person and self._player_num:
            scode /= self._player_num
        if self._scode_divide_time:
            minutes = self.game_time_sec / 60.0
            if minutes > 0:
                scode /= minutes
        return round(scode, 2)

    def reset_for_level(self):
        """Clear PER-LEVEL board state before loading the next level.
        Score, score2, life, session timer all PERSIST (not reset)."""
        self.flashes = {}
        self.scored_active = set()
        self.scored_active2 = set()
        self.pending_respawn = []
        self.goal_cells = set()
        self.goal2_cells = set()
        self.red_cells = set()
        self.deduct_cells = set()
        self.last_life_loss_time = 0.0
        self._cell_red_penalty_at.clear()
        self.green_cells = set()
        self._level_cleared = False
        self._restart_level = False
        self._saw_scoreable = False
        self._level_time_pass = 0.0
        self._wave_skip_at = None

    def _current_level_time(self) -> float:
        """Level timeline for consume/scoring (Play.total_pass)."""
        if self.play is not None:
            return float(getattr(self.play, "total_pass", self._level_time_pass))
        return self._level_time_pass

    def _live_cell_category(self, i: int, j: int) -> str:
        """Classify (i,j) at press time using current group positions.

        Frame-cached goal_cells can lag on fast-moving levels (e.g. --/24.led);
        scoring must use live dict_group + total_pass like the original game."""
        if not self.dict_group:
            return "decor"
        try:
            from model.setting import Setting
        except ImportError:
            return "decor"

        tp = self._current_level_time()
        ri, rj = int(i), int(j)
        _P1 = (0, 0, 254)
        _P2 = (254, 128, 0)
        _GREEN = (0, 254, 0)
        _scoreset = set(_HOOPS_COLOR_ARR)
        best_rank = -1
        best_cat = "decor"

        for g in self.dict_group.values():
            sm = getattr(g, "start_member", None)
            if not sm:
                continue
            if getattr(g, "type", None) != Setting.FLOOR_LIGHT:
                continue
            if not (g.start_time_sec <= tp <= g.end_time_sec):
                continue
            if not any(round(c[0]) == ri and round(c[1]) == rj for c in sm):
                continue
            mc = _group_main_color(g.color)
            if mc == _GREEN:
                rank, cat = 3, "green"
            elif _rgb_is_deduct(mc):
                rank, cat = 2, "deduct"
            elif mc in _HOOPS_HAZARD_COLORS:
                rank, cat = 2, "red"
            elif self.multiplayer and mc == _P1:
                rank, cat = 1, "p1"
            elif self.multiplayer and mc == _P2:
                rank, cat = 1, "p2"
            elif (not self.multiplayer) and mc in _scoreset:
                rank, cat = 1, "goal"
            else:
                rank, cat = 0, "decor"
            if rank > best_rank:
                best_rank, best_cat = rank, cat
        return best_cat

    def is_expired(self) -> bool:
        """Check if game timed out"""
        elapsed = time.time() - self.created_at
        return elapsed > GAME_TIMEOUT_SECONDS

    def get_state(self) -> dict:
        """Get current game state"""
        return self.current_state

    def update_state(self, **kwargs):
        """Update game state"""
        self.current_state.update(kwargs)

    def begin_level_transition(self):
        """Lock input while a transition panel runs."""
        _set_input_acceptance(self, False)

    def finish_level_transition(self):
        """Transition panel finished; gameplay re-enables input in _run_level_attempt."""
        pass

    def try_score_cell(self, i, j):
        """Type-aware scoring for a press on cell (i,j):
          - red hazard cell  -> -1 point + -1 HP (HP rate-limited)
          - goal_led target  -> +1 point + consume (tile blanks) + flash
          - background decor  -> nothing (neutral)
        Uses live group positions so moving levels score reliably."""
        cat = self._live_cell_category(i, j)

        if cat == "green":
            return
        if cat == "red":
            now = time.time()
            last = self._cell_red_penalty_at.get((i, j), 0.0)
            if now - last >= self._life_count_time:
                self.score -= 1
                if self.multiplayer:
                    self.score2 -= 1
                self.life -= 1
                self._cell_red_penalty_at[(i, j)] = now
                self.flashes[(i, j)] = now
                mgr = getattr(self, "_audio_mgr", None)
                if mgr:
                    mgr.play_score_negative()
            return
        if cat == "deduct" and (i, j) not in self.scored_active:
            self.scored_active.add((i, j))
            self.score -= 1
            self.flashes[(i, j)] = time.time()
            mgr = getattr(self, "_audio_mgr", None)
            if mgr:
                mgr.play_score_negative()
            self._consume_cell(i, j, for_deduct=True)
            return

        in_p1 = cat in ("goal", "p1")
        in_p2 = cat == "p2"

        # P1 goal: score + consume
        if in_p1 and (i, j) not in self.scored_active:
            self.scored_active.add((i, j))
            self.score += 1
            mgr = getattr(self, "_audio_mgr", None)
            if mgr:
                mgr.play_score_positive()
            self._consume_cell(i, j)
            return
        # P2 goal: separate score + consume
        if in_p2 and (i, j) not in self.scored_active2:
            self.scored_active2.add((i, j))
            self.score2 += 1
            mgr = getattr(self, "_audio_mgr", None)
            if mgr:
                mgr.play_score_positive()
            self._consume_cell(i, j)
            return
        # else: background decor — neutral, no effect.

    def _consume_cell(self, i, j, *, for_deduct: bool = False):
        """Remove a scored cell from active in-time groups only.

        Matches gui_editor_game.calculation_editor_group_scode: a press
        removes the cell from scoreable (or deduct) groups whose time
        window contains NOW — never from green/red or future waves."""
        tp = self._current_level_time()
        if not self.dict_group:
            return
        try:
            from model.setting import Setting
        except ImportError:
            return

        _P1 = (0, 0, 254)
        _P2 = (254, 128, 0)
        if for_deduct:
            color_ok = _rgb_is_deduct
        elif self.multiplayer:
            allowed = {_P1, _P2}
            color_ok = lambda mc: mc in allowed
        else:
            allowed = set(_HOOPS_COLOR_ARR)
            color_ok = lambda mc: mc in allowed

        ri, rj = int(i), int(j)
        for g in self.dict_group.values():
            if getattr(g, "type", None) != Setting.FLOOR_LIGHT:
                continue
            st = getattr(g, "start_time_sec", 0)
            en = getattr(g, "end_time_sec", 0)
            if not (st < tp < en):
                continue
            mc = _group_main_color(g.color)
            if not color_ok(mc):
                continue
            sm = getattr(g, "start_member", None)
            if not sm:
                continue
            for coord in list(sm):
                if round(coord[0]) == ri and round(coord[1]) == rj:
                    try:
                        if isinstance(sm, set):
                            sm.discard(coord)
                        else:
                            sm.remove(coord)
                    except (KeyError, ValueError, TypeError):
                        pass

    def process_respawns(self):
        """Re-add consumed 2P goal tiles after respawn_delay. Per-frame."""
        if not self.pending_respawn:
            return
        now = time.time(); still = []
        for entry in self.pending_respawn:
            g, cell, t = entry
            if now >= t:
                sm = getattr(g, "start_member", None)
                try:
                    if isinstance(sm, set): sm.add(cell)
                    elif sm is not None and cell not in sm: sm.append(cell)
                except Exception:
                    pass
            else:
                still.append(entry)
        self.pending_respawn = still

    def apply_input(self, row: int, col: int, action: str):
        """Player input from simulator: press/release a tile.
        Press scores immediately if the tile is lit (mouse clicks are
        instantaneous, so we can't wait for the next frame)."""
        request_epoch = self._input_epoch
        with self.input_lock:
            if (
                not self.accepting_input
                or request_epoch != self._input_epoch
                or self.led_table is None
            ):
                return False
            # Ignore presses outside the level's active zone (e.g. 5x9).
            z = self.zone
            if z and not (z[0] <= row < z[1] and z[2] <= col < z[3]):
                return False
            if action == "press":
                self.led_table.press_cell(row, col)
                self.try_score_cell(row, col)  # score on press (instant clicks)
            elif action == "release":
                self.led_table.release_cell(row, col)
        return True


class GameManager:
    """Manages all running game instances"""

    def __init__(self):
        self.games: Dict[str, GameInstance] = {}
        self.lock = threading.Lock()
        self._create_lock = threading.Lock()
        self.zombie_threads = []
        logger.info("GameManager initialized")

    def clear_all(self):
        """Stop and remove all existing games. Joins threads (3s timeout) before clearing."""
        with self.lock:
            for gid, g in list(self.games.items()):
                g.running = False
            threads = [(gid, g.thread) for gid, g in self.games.items() if getattr(g, "thread", None)]
            led_tables = [getattr(g, "led_table", None) for g in self.games.values()]
            self.games.clear()
        for gid, t in threads:
            t.join(timeout=3.0)
            if t.is_alive():
                logger.warning(f"Thread {gid} didn't stop in 3s — zombie")
                self.zombie_threads.append(gid)
        # Blank the physical floor for every cleared game — a stopped/cleared
        # game leaves its last drawn frame on the hardware otherwise (see
        # docs/TODO_HARDWARE_BLANK_ON_STOP.md).
        for led_table in led_tables:
            _hw_blank_floor(led_table)
        logger.info("Cleared all existing games")

    def create_game(self, card_id: str, level: int, difficulty: str,
                    player_count: int = 1, mode: str = None) -> str:
        """Create new game instance. Clears any prior games first (kiosk model)."""
        self.clear_all()
        with self.lock:
            game_id = str(uuid.uuid4())[:8]
            # Group mode forces 1P behavior
            if (mode or "").strip().lower() == "group":
                player_count = 1
            game = GameInstance(
                game_id, card_id, level, difficulty, player_count, mode=mode
            )
            if game.mode == "group":
                logger.info(
                    f"Game created: {game_id} mode=group "
                    f"(card={card_id}, level={level})"
                )
            else:
                logger.info(
                    f"Game created: {game_id} (card={card_id}, level={level}, "
                    f"players={game.session_player_count})"
                )
            self.games[game_id] = game
            return game_id

    def get_game(self, game_id: str) -> Optional[GameInstance]:
        """Get game by ID"""
        with self.lock:
            return self.games.get(game_id)

    def start_game(self, game_id: str):
        """Start game loop in background thread"""
        game = self.get_game(game_id)
        if not game:
            raise ValueError(f"Game not found: {game_id}")

        def _run_game():
            try:
                # Reinstall mocks in this thread (sim mode only)
                if not USE_SERIAL_HD:
                    if 'serial' not in sys.modules:
                        sys.modules['serial'] = MagicMock()
                    if 'led' not in sys.modules:
                        sys.modules['led'] = MagicMock()
                    if 'led.led_control' not in sys.modules:
                        sys.modules['led.led_control'] = MagicMock()
                else:
                    # HW init may fail without COM (local dev); continue with
                    # HeadlessLedTable + sim — same as Climb/Grid/Hex.
                    if _hw_init() is None:
                        logger.warning(
                            f"Hardware init failed for {game_id}; "
                            "continuing with simulator-only play (no floor draws)"
                        )

                logger.info(f"Starting game loop: {game_id}")

                # Import game modules (with fallback to mock loop on import error)
                Play = None
                LedTable = None
                Setting = None
                try:
                    logger.info(f"Importing game modules for {game_id}")
                    import shelve
                    import os
                    from game_play.Play import Play
                    from game_play.game_running import LedTable
                    from model.setting import Setting
                    logger.info(f"✓ Game modules imported")
                except Exception as import_err:
                    logger.warning(f"Game module import failed, using mock loop: {import_err}")
                    import traceback
                    logger.warning(f"Import traceback: {traceback.format_exc()}")
                    Play = None
                    LedTable = None
                    Setting = None
                    # If imports failed, force dict_group to None to skip to mock loop
                    dict_group = None

                # Initialize game components — always use HeadlessLedTable
                # (never the mocked gui2 LedTable — that's MagicMock, comparisons fail)
                _s = load_real_settings()
                _rows = _s.get("grid_rows", 1)
                _cols = _s.get("grid_cols", 6)
                led_table = HeadlessLedTable(wall_light_arr_len=100, led_row=_rows, led_col=_cols)
                logger.info(f"HeadlessLedTable ready: {led_table.led_row}x{led_table.led_col} (Hoops hoop strip)")

                # Create mock settings object with required attributes
                # Climb's Play.__init__ expects setting.leval_span.get(), setting.blue_hide_max_time.get(), etc.
                _s_boot = load_real_settings()
                class MockSetting:
                    def __init__(self):
                        class MockAttr:
                            def __init__(self, val):
                                self._val = val
                            def get(self):
                                return self._val
                        self.leval_span = MockAttr(_s_boot.get("leval_span", 0.8))
                        self.blue_hide_max_time = MockAttr(_s_boot.get("blue_hide_max_time", 20.0))
                        self.corner_line_start = MockAttr(0)

                mock_setting = MockSetting()

                # Create dummy callback (Play expects partial_fun_cb for UI updates)
                def dummy_callback(*args, **kwargs):
                    pass

                # game_level: numeric difficulty (1=easy, 2=normal, 3=hard), not level ID
                difficulty_map = {"easy": 1, "normal": 2, "hard": 3}
                game_level_num = difficulty_map.get(game.difficulty, 2)  # default to normal

                play = None
                try:
                    logger.debug(f"Creating Play instance for game {game_id}")
                    play = Play(led_table, mock_setting, dummy_callback, game_level=game_level_num)
                    # Sweep speed scale: time-per-cell = group.speed / game_level_speed.
                    # ONLY affects MOVING groups (group.speed != 0); all-static levels
                    # (e.g. hoops 001/003) ignore it. Now that Play.running/running_by_blue
                    # correctly gate cell-steps on accumulated move_distance (see Play.py),
                    # this value gives a real, meaningful cadence — e.g. at speed=2.0,
                    # normal=0.4 -> one cell every 2.0/0.4 = 5s. TUNE HERE if moving
                    # levels still feel too fast/slow.
                    difficulty_speed = {"easy": 0.27, "normal": 0.4, "hard": 0.6}
                    play.game_level_speed = difficulty_speed.get(game.difficulty, 0.4)
                    if game.session_player_count >= 2:
                        play.game_level_speed *= 0.65  # DK levels: slower sweep, less strobe
                except Exception as e:
                    logger.warning(f"Play creation failed, using mock loop: {e}")
                    play = None

                # ── SESSION SETUP ────────────────────────────────────────────
                # Build the level marathon sequence from the chosen start level
                # to the end of its series (A001..A025 / B01..B31 / DK01..DK10).
                game.play = play
                game.led_table = led_table          # expose for press input
                game.session_start = time.time()
                import datetime as _dt
                game.update_state(started_at=_dt.datetime.now().isoformat(timespec="seconds"))
                game._end_reason = None
                if game.mode == "group":
                    game.level_sequence = _build_group_level_sequence(game.level)
                    # Sync display start level to first playlist entry
                    if game.level_sequence:
                        game.level = os.path.basename(
                            game.level_sequence[0]
                        ).rsplit(".", 1)[0]
                    game.session_player_count = 1
                    game.multiplayer = False
                else:
                    game.level_sequence = _build_level_sequence(game.level)
                logger.info(
                    f"Session: {len(game.level_sequence)} levels from "
                    f"'{game.level}' mode={game.mode or 'single'} (5-min marathon)"
                )

                game_start_time = game.session_start  # legacy alias for mock loop

                BLACK3 = [(0, 0, 0), (0, 0, 0), (0, 0, 0)]

                def _ensure_anim(g):
                    """Pickled groups lack breath state; init it lazily so
                    group.breath() works (shimmer effect)."""
                    if not isinstance(getattr(g, "breath_color_float", None), list) \
                            or not (g.breath_color_float and isinstance(g.breath_color_float[0], (list, tuple))):
                        col = g.color if (isinstance(g.color, (list, tuple)) and g.color
                                          and isinstance(g.color[0], (list, tuple))) else BLACK3
                        g.breath_color = [list(c) for c in col]
                        g.breath_color_float = [list(c) for c in col]
                        g.breath_switch = [True, True, True]
                    if not hasattr(g, "trigger_span_tm"):
                        g.trigger_span_tm = 0

                def _setup_level(dg, go, lvl_path):
                    """Configure game state for a freshly-loaded level. Score,
                    score2, life, session timer all PERSIST (set elsewhere)."""
                    game.dict_group = dg
                    # Per-level board time = max group end_time.
                    try:
                        game.board_time_sec = max(
                            (getattr(g, "end_time_sec", 0) for g in dg.values()),
                            default=1e9)
                    except Exception:
                        game.board_time_sec = 1e9
                    lr = _s["grid_rows"]
                    lc = _s["grid_cols"]
                    led_table.resize(lr, lc)
                    if play is not None:
                        play.obj_led_table = led_table
                    # Play zone (guards input).
                    if go is not None:
                        try:
                            game.zone = (int(getattr(go, "zone_row_from", 0)),
                                         int(getattr(go, "zone_row_to", lr)),
                                         int(getattr(go, "zone_col_from", 0)),
                                         int(getattr(go, "zone_col_to", lc)))
                        except Exception:
                            game.zone = None
                    # Multiplayer split scoring: .ledb + 2-player session only.
                    game.multiplayer = _level_uses_2p_scoring(
                        lvl_path, game.session_player_count)
                    game._player_num = 2 if game.multiplayer else 1
                    # Original EditorGame: cover_action False → disappear mode.
                    game._cover_disappear = not bool(
                        getattr(go, "cover_action", False)) if go else True
                    game._play_order = bool(getattr(go, "play_order", False)) if go else False
                    # Init breath/anim state for all groups.
                    for g in dg.values():
                        try:
                            _ensure_anim(g)
                        except Exception:
                            pass

                # Per-frame callback fired by Play.update() inside Play.running().
                # By this point Play has: moved groups (deal_all_direction by
                # speed), advanced total_pass, cleared+redrawn led_table for the
                # current frame. We score presses and publish the frame.
                # Returning False makes Play.running() stop (timeout / Stop btn).
                frame_counter = {"n": 0}

                def _frame_callback(play_self, dgroup, time_pass, total_pass):
                    # total_pass is PER-LEVEL (reset each level). Session timing
                    # is wall-clock from game.session_start.
                    try:
                        session_elapsed = time.time() - game.session_start
                        game._level_time_pass = total_pass

                        # ── SESSION-END conditions (stop the whole marathon) ──
                        #   life<=0          -> result 0 (out of lives)
                        #   session timer up -> result 2 (5-min timeout)
                        if game.life <= 0:
                            time_left = game.game_time_sec - session_elapsed
                            if time_left > 10.0:
                                # Lives gone but time remains: restart same level,
                                # keep score. Session loop refills HP and replays.
                                game._restart_level = True
                                return False
                            game._session_over = True
                            game.update_state(game_over_reason="out_of_life", result=0)
                            return False
                        if not game.running:
                            return _handle_stopped_session(game)
                        if session_elapsed > game.game_time_sec:
                            game._session_over = True
                            game._end_reason = "timeout"
                            game.update_state(game_over_reason="timeout", result=2)
                            return False
                        # ── LEVEL-END by TIME (advance to next level) ──
                        # Matches original EditorGame: level runs until
                        # total_pass > cur_game_time (max group end_time).
                        if total_pass > game.board_time_sec:
                            game._level_cleared = True
                            return False

                        # Hoops: hide scoreable tiles under cover (disappear mode only).
                        if game._cover_disappear:
                            _hoops_apply_cover_disappear(
                                dgroup, total_pass, game._blue_hide_max_time)

                        # ── HOOPS CLASSIFICATION ─────────────────────────────
                        # 1×N hoop strip: each column is one backboard.
                        # Scoreable = PLUS_ARR colors; RED/DEDUCT = penalty.
                        _P1_COLOR = (0, 0, 254)    # blue   (P1)
                        _P2_COLOR = (254, 128, 0)  # orange (P2)
                        (
                            cell_win,
                            goal_cells,
                            goal2_cells,
                            red_cells,
                            deduct_cells,
                            green_cells,
                        ) = _classify_hoops_frame(
                            dgroup, total_pass, led_table, game.multiplayer
                        )

                        game.goal_cells  = goal_cells
                        game.goal2_cells = goal2_cells
                        game.red_cells   = red_cells
                        game.deduct_cells = deduct_cells
                        game.green_cells = green_cells

                        # Remember that scoreable tiles appeared — so an empty
                        # board later means "all collected", not "not started".
                        if goal_cells or goal2_cells:
                            game._saw_scoreable = True

                        # ── WAVE SKIP (scoreable-only) ───────────────────────
                        # When the current scoreable wave is cleared, skip dead
                        # time to the next wave. running_by_blue can't do this
                        # while red/green groups are still in-window; ignore them.
                        _scoreable_colors = ({_P1_COLOR, _P2_COLOR} if game.multiplayer
                                             else set(_HOOPS_COLOR_ARR))
                        if total_pass > 0.5 and not goal_cells and not goal2_cells:
                            active_scoreable_now = 0
                            next_wave_start = None
                            for g in dgroup.values():
                                if getattr(g, "type", None) != Setting.FLOOR_LIGHT:
                                    continue
                                mc = _group_main_color(g.color)
                                if mc not in _scoreable_colors:
                                    continue
                                sm = getattr(g, "start_member", None)
                                if not sm:
                                    continue
                                st = g.start_time_sec
                                en = g.end_time_sec
                                if st <= total_pass <= en:
                                    active_scoreable_now += len(sm)
                                elif st > total_pass and (
                                        next_wave_start is None or st < next_wave_start):
                                    next_wave_start = st
                            if active_scoreable_now == 0 and next_wave_start is not None \
                                    and (next_wave_start - total_pass) >= 0.05:
                                if game._wave_skip_at is None:
                                    game._wave_skip_at = total_pass + WAVE_SKIP_DELAY_SEC
                                elif total_pass >= game._wave_skip_at:
                                    logger.debug(
                                        f"Wave skip: {total_pass:.1f}s -> "
                                        f"{next_wave_start:.1f}s")
                                    play_self.total_pass = next_wave_start
                                    game._level_time_pass = next_wave_start
                                    game._wave_skip_at = None
                                    return True
                            elif active_scoreable_now == 0 and next_wave_start is None \
                                    and game._saw_scoreable:
                                # All scoreable tiles collected/expired and no more
                                # waves this level → level cleared, advance to next.
                                logger.info(f"✓ All scoreable tiles done at "
                                            f"{total_pass:.1f}s → level clear")
                                game._level_cleared = True
                                return False
                            else:
                                game._wave_skip_at = None

                        # DEBUG: log tile classification every 60 frames
                        if frame_counter["n"] % 60 == 0:
                            logger.debug(f"Frame {frame_counter['n']}: goal={len(goal_cells)}, "
                                         f"goal2={len(goal2_cells)}, red={len(red_cells)}, "
                                         f"deduct={len(deduct_cells)}, t={total_pass:.1f}, "
                                         f"mp={game.multiplayer}")

                        # 2) SCORE pressed cells (type-aware). Drop scored marks
                        #    for goals that are no longer active so they can score
                        #    again if they reappear.
                        with game.input_lock:
                            if game.multiplayer:
                                game.process_respawns()
                            # Keep scored marks for ACTIVE goal/deduct cells only.
                            # Deduct cells must stay in scored_active while pressed
                            # or they fire every single frame (life drain per frame).
                            active_consumables = goal_cells | goal2_cells | deduct_cells
                            game.scored_active &= active_consumables
                            game.scored_active2 &= goal2_cells
                            _score_pressed_hoops_cells(game, led_table)

                        # 2) Build display buffer from the PRIORITY winner map so
                        #    overlapping cells render the WINNING color (green >
                        #    red/deduct > blue/orange), matching interaction.
                        #    Single RGB per cell (Climb = square single-color).
                        led_display = _build_hoops_led_display(
                            cell_win,
                            led_table,
                            total_pass,
                            game.flashes,
                        )

                        # ── HARDWARE I/O ─────────────────────────────────────
                        _now = time.time()
                        if USE_SERIAL_HD and _hw_led_control is not None and \
                                _now - getattr(game, "_hw_last_draw", 0) >= _HW_DRAW_INTERVAL:
                            with _hw_serial_lock:
                                try:
                                    _write_hoops_hardware_frame(
                                        game,
                                        _hw_led_control,
                                        _hw_layout_type,
                                        led_table,
                                        led_display,
                                        draw_time=_now,
                                    )
                                except Exception as _hw_err:
                                    logger.warning(f"HW I/O: {_hw_err}")

                        game.update_state(
                            score=game.score,
                            score2=game.score2,
                            multiplayer=game.multiplayer,
                            time_elapsed=session_elapsed,                       # SESSION elapsed
                            time_left=max(0, game.game_time_sec - session_elapsed),  # SESSION countdown
                            life=game.life,
                            display_lives=math.ceil(game.life / 4),          # 5 hearts, 4 mistakes each
                            display_max=math.ceil(game.max_life / 4),
                            game_over=False,
                            led_display=led_display,
                            grid_rows=led_table.led_row,
                            grid_cols=led_table.led_col,
                            current_level=game.current_level_id,
                            levels_cleared=game.levels_cleared,
                            phase="playing",
                            accepting_input=True,
                            backend_audio=_backend_audio_active(
                                getattr(game, "_audio_mgr", None)
                            ),
                        )

                        frame_counter["n"] += 1
                        if frame_counter["n"] % 120 == 0:
                            logger.debug(f"Game {game_id}: score={game.score}, "
                                         f"t={total_pass:.1f}s")
                        # Pace ~100fps. running() is a tight loop with no sleep;
                        # wall-clock timing keeps movement correct regardless.
                        time.sleep(0.01)
                        return True
                    except Exception as cb_err:
                        return _handle_frame_callback_error(game, game_id, cb_err)

                # ── SESSION LOOP ─────────────────────────────────────────────
                # Marathon through level_sequence. Score + lives + 5-min timer
                # persist across levels. Each level runs via Play.running() until
                # the callback returns False (level cleared -> advance, or session
                # over -> stop). End on life<=0, timer<=0, or sequence exhausted.
                if play is None or not game.level_sequence:
                    logger.warning(f"No Play object or empty level sequence; "
                                   f"session cannot run: {game_id}")
                    _mark_session_error(game, "no_levels")
                    game.update_state(game_over=True, time_left=0)
                    _hw_blank_floor(getattr(game, "led_table", None))
                    game.running = False
                    return

                play.callback = _frame_callback
                audio_mgr = None
                try:
                    from api.audio_manager import AudioManager

                    audio_mgr = AudioManager(settings=_s, enabled=False)
                except Exception as aud_err:
                    logger.warning(f"AudioManager unavailable: {aud_err}")
                game._audio_mgr = audio_mgr

                effect_countdown = _effect_path("countdown")
                effect_clear = _effect_path("level_clear")
                effect_fail = _effect_path("level_fail")

                for lvl_index, lvl_path in enumerate(game.level_sequence):
                    if game._session_over:
                        break
                    if not game.running:
                        _handle_stopped_session(game)
                        _finish_session(
                            game,
                            play,
                            led_table,
                            audio_mgr,
                            reason="stopped",
                            settings=_s,
                        )
                        break
                    session_elapsed = time.time() - game.session_start
                    if session_elapsed > game.game_time_sec:
                        _finish_session(
                            game,
                            play,
                            led_table,
                            audio_mgr,
                            reason="timeout",
                            settings=_s,
                        )
                        break

                    lvl_id = os.path.basename(lvl_path).rsplit(".", 1)[0]

                    _play_led_panel(
                        game,
                        play,
                        led_table,
                        effect_countdown,
                        phase="countdown",
                        settings=_s,
                        audio_mgr=audio_mgr,
                        countdown_ticks=True,
                    )

                    while True:
                        game.current_level_id = lvl_id
                        if audio_mgr:
                            audio_mgr.start_bgm()
                        try:
                            prepared = _run_level_attempt(
                                lvl_path, game, _s, _setup_level, play
                            )
                        except Exception as run_err:
                            import traceback

                            logger.warning(
                                f"Level {lvl_id} run error: {run_err}\n"
                                f"{traceback.format_exc()}"
                            )
                            _mark_level_error(game)
                            break
                        finally:
                            if audio_mgr:
                                audio_mgr.stop_bgm()

                        if prepared is None:
                            break

                        if game._session_over:
                            reason = (
                                game._end_reason
                                or game.get_state().get("game_over_reason")
                                or "timeout"
                            )
                            if game.life <= 0 and reason not in ("timeout", "stopped"):
                                reason = "out_of_life"
                            _finish_session(
                                game,
                                play,
                                led_table,
                                audio_mgr,
                                reason=reason,
                                settings=_s,
                                play_clear=reason
                                in ("timeout", "out_of_life", "stopped"),
                            )
                            break

                        if game._restart_level:
                            _play_led_panel(
                                game,
                                play,
                                led_table,
                                effect_fail,
                                phase="level_fail",
                                settings=_s,
                                audio_mgr=audio_mgr,
                                stinger=getattr(
                                    audio_mgr, "transition_stinger", None
                                )
                                if audio_mgr
                                else None,
                            )
                            game.life = game.max_life
                            game._cell_red_penalty_at.clear()
                            game.last_life_loss_time = 0.0
                            game._restart_level = False
                            logger.info(
                                f"↻ Life restart: level={lvl_id}, score={game.score}"
                            )
                            _play_led_panel(
                                game,
                                play,
                                led_table,
                                effect_countdown,
                                phase="countdown",
                                settings=_s,
                                audio_mgr=audio_mgr,
                                countdown_ticks=True,
                            )
                            continue

                        if game._level_cleared:
                            game.levels_cleared += 1
                            logger.info(
                                f"✓ Level {lvl_id} cleared "
                                f"(total cleared={game.levels_cleared})"
                            )
                            _play_led_panel(
                                game,
                                play,
                                led_table,
                                effect_clear,
                                phase="level_clear",
                                settings=_s,
                                audio_mgr=audio_mgr,
                                stinger=getattr(
                                    audio_mgr, "transition_stinger", None
                                )
                                if audio_mgr
                                else None,
                            )
                            has_next = lvl_index + 1 < len(game.level_sequence)
                            session_elapsed = time.time() - game.session_start
                            if (
                                not has_next
                                or session_elapsed > game.game_time_sec
                            ):
                                _finish_session(
                                    game,
                                    play,
                                    led_table,
                                    audio_mgr,
                                    reason="sequence_done",
                                    settings=_s,
                                    play_clear=False,
                                )
                            break

                        break

                    if game._session_over:
                        break

                # Session finished (timer/lives/sequence end).
                game._session_over = True
                # Result honesty: 1 = cleared the whole level chain within time,
                # 2 = ran out of session time, 0 = out of life. Only a genuine
                # chain-exhaustion (loop finished with no timeout/out-of-life
                # reason) counts as "complete".
                final_result, final_reason = _resolve_session_outcome(game)
                final_score = game.compute_final_score(game.score)
                final_score2 = game.compute_final_score(game.score2)
                logger.info(f"Session over: reason={final_reason}, "
                            f"raw_score={game.score} -> final={final_score}, "
                            f"raw_score2={game.score2} -> final2={final_score2}, "
                            f"levels_cleared={game.levels_cleared}")
                game.update_state(game_over=True, time_left=0,
                                  game_over_reason=final_reason, result=final_result,
                                  levels_cleared=game.levels_cleared,
                                  final_score=final_score, final_score2=final_score2)
                # Session over (timeout/lives/sequence-exhausted) — blank the
                # physical floor so it doesn't stay lit on the last frame
                # (see docs/TODO_HARDWARE_BLANK_ON_STOP.md).
                if not getattr(game, "_floor_blanked", False):
                    _hw_blank_floor(getattr(game, "led_table", None))
                game.running = False

            except Exception as e:
                import traceback
                logger.error(f"Game error {game_id}: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                game.running = False
                _mark_session_error(game, "game_error")
                _hw_blank_floor(getattr(game, "led_table", None))
                game.update_state(
                    game_over=True,
                    game_over_reason="game_error",
                    result=0,
                )

        game.running = True   # set synchronously — clear_all() won't skip this thread
        game._sim_pressed = set()
        game.thread = threading.Thread(target=_run_game, daemon=True)
        game.thread.start()

    def stop_game(self, game_id: str) -> dict:
        """Stop game and return final state"""
        game = self.get_game(game_id)
        if not game:
            return {"success": False, "error": f"Game not found: {game_id}"}

        game.running = False
        if game.thread:
            game.thread.join(timeout=5)

        # Blank the physical floor — user-initiated stop otherwise leaves the
        # last drawn frame lit on real hardware (see
        # docs/TODO_HARDWARE_BLANK_ON_STOP.md).
        _hw_blank_floor(getattr(game, "led_table", None))

        final_state = game.get_state()

        with self.lock:
            del self.games[game_id]

        logger.info(f"Game stopped: {game_id}")
        return {"success": True, "state": final_state}

    def cleanup_expired(self):
        """Remove expired games"""
        with self.lock:
            expired = [gid for gid, game in self.games.items() if game.is_expired()]
            for gid in expired:
                del self.games[gid]
                logger.warning(f"Game expired and removed: {gid}")

    def get_stats(self) -> dict:
        """Get manager statistics"""
        with self.lock:
            return {
                "active_games": len(self.games),
                "max_games": MAX_CONCURRENT_GAMES,
                "timeout_seconds": GAME_TIMEOUT_SECONDS
            }


# Global instance
_manager = None

def get_manager() -> GameManager:
    """Get GameManager singleton"""
    global _manager
    if _manager is None:
        _manager = GameManager()
    return _manager
