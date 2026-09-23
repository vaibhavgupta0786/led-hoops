"""
LED Play Game API - FastAPI Backend
Decoupled game logic from Tkinter UI. React frontend connects via HTTP + WebSocket.
"""
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import os
from loguru import logger
import datetime

from .config import API_HOST, API_PORT, API_DEBUG, GAME_NAME, GAMES_ROOT, UI_DIR
from .models import (
    LoginRequest, LoginResponse, PlayerInfo,
    StartGameRequest, StartGameResponse,
    GameState, GameInput, GameResult, LogoutRequest, LogoutResponse
)
from .database import get_db
from .game_manager import get_manager

# Setup logging
logger.add("logs/api.log", rotation="500 MB", level="INFO")
logger.info(f"Starting API for game: {GAME_NAME}")

# FastAPI app
app = FastAPI(
    title=f"LED Play API - {GAME_NAME}",
    version="1.0.0",
    debug=API_DEBUG
)

# CORS middleware (for React frontend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Get singletons
db = get_db()
game_manager = get_manager()


# ============= LOGIN ENDPOINT =============
@app.post("/login")
async def login(request: LoginRequest) -> LoginResponse:
    """
    Lookup player by RFID card ID.
    Returns player info or error.
    """
    logger.info(f"Login request: card_id={request.card_id}")

    try:
        player_data = db.get_player_by_card(request.card_id)

        if not player_data:
            logger.warning(f"Card not found: {request.card_id}")
            return LoginResponse(
                success=False,
                error="Card not recognized"
            )

        # Parse player data (varies by index, check db_operation.py)
        # Format: (custom_id, phone, name, public, time_left, pwd, card_id, ...)
        player_info = PlayerInfo(
            custom_id=int(player_data[0]),
            phone=str(player_data[1]),
            name=str(player_data[2]),
            time_left=float(player_data[4]),
            card_id=request.card_id
        )

        logger.info(f"Login success: {player_info.name}")
        return LoginResponse(success=True, player=player_info)

    except Exception as e:
        logger.error(f"Login error: {e}")
        return LoginResponse(success=False, error=str(e))


# ============= START GAME ENDPOINT =============
@app.post("/start-game")
async def start_game(request: StartGameRequest) -> StartGameResponse:
    """
    Start new game instance.
    Returns game_id and WebSocket URL for real-time updates.
    """
    logger.info(f"Start game request: card={request.card_id}, level={request.level}")

    try:
        # Check session remaining time (60-min timer)
        time_left = db.check_session_remaining(request.card_id)
        if time_left is not None and time_left <= 0:
            logger.warning(f"Session expired for card: {request.card_id}")
            return StartGameResponse(
                success=False,
                error="Session time expired (60-minute limit)"
            )

        mode = (request.mode or "").strip().lower() or None
        if mode and mode != "group":
            return StartGameResponse(success=False, error=f"Unknown mode: {mode}")

        level = request.level
        if mode == "group" and (level is None or str(level).strip() == ""):
            level = "auto"

        # Group mode is always 1P (.led under source_group/)
        player_count = 1 if mode == "group" else request.player_count

        # Create game instance
        game_id = game_manager.create_game(
            request.card_id,
            level if level is not None else "auto",
            request.difficulty,
            player_count=player_count,
            mode=mode,
        )

        # Start game loop in background
        game_manager.start_game(game_id)

        ws_url = f"/game/{game_id}"
        logger.info(f"Game started: {game_id}")

        return StartGameResponse(
            success=True,
            game_id=game_id,
            ws_url=ws_url
        )

    except Exception as e:
        logger.error(f"Start game error: {e}")
        return StartGameResponse(success=False, error=str(e))


# ============= GAME STATE WEBSOCKET =============
@app.websocket("/game/{game_id}")
async def game_websocket(websocket: WebSocket, game_id: str):
    """
    WebSocket for real-time game state streaming.
    Game → React: score, LEDs, game state updates
    React → Game: input events (button presses, etc.)
    """
    logger.info(f"WebSocket connected: {game_id}")

    await websocket.accept()
    game = game_manager.get_game(game_id)

    if not game:
        await websocket.close(code=1008, reason="Game not found")
        return

    try:
        while game.running:
            # Send game state to client
            state = game.get_state()
            await websocket.send_json({
                "type": "game_state",
                "data": state
            })

            # Check for input from client
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=0.05)
                input_msg = json.loads(data)
                logger.debug(f"Input received: {input_msg}")
                # TODO: Feed input to Play.py
            except asyncio.TimeoutError:
                pass  # No input, continue

            await asyncio.sleep(0.016)  # ~60fps

        # Game ended
        await websocket.send_json({
            "type": "game_state",
            "data": {**game.get_state(), "game_over": True}
        })

    except Exception as e:
        logger.error(f"WebSocket error {game_id}: {e}")
    finally:
        await websocket.close()
        logger.info(f"WebSocket closed: {game_id}")


# ============= RESULT ENDPOINT =============
@app.get("/result/{game_id}")
async def get_result(game_id: str) -> GameResult:
    """
    Get game result and leaderboard after game ends.
    """
    logger.info(f"Result request: {game_id}")

    try:
        game = game_manager.get_game(game_id)

        if game:
            # Game still exists (not cleaned up yet)
            final_state = game.get_state()
        else:
            # Game already cleaned up, return last known state
            final_state = {}

        # Get leaderboard
        leaderboard = db.get_leaderboard(game_id, limit=10)

        result = GameResult(
            success=True,
            score=final_state.get("score", 0),
            player_name=final_state.get("player_name", "Unknown"),
            time_used=final_state.get("time_elapsed", 0.0),
            difficulty="normal",  # TODO: get from game
            leaderboard=leaderboard
        )

        logger.info(f"Result returned: score={result.score}")
        return result

    except Exception as e:
        logger.error(f"Result error: {e}")
        return GameResult(success=False, error=str(e))


# ============= LOGOUT ENDPOINT =============
@app.post("/logout")
async def logout(request: LogoutRequest) -> LogoutResponse:
    """
    End game session and record score.
    """
    logger.info(f"Logout request: game_id={request.game_id}, card={request.card_id}")

    try:
        result = game_manager.stop_game(request.game_id)

        if result["success"]:
            # Record score to database
            score_data = {
                "card_id": request.card_id,
                "game_id": request.game_id,
                "score": result["state"].get("score", 0),
                "timestamp": datetime.datetime.now().isoformat()
            }
            db.record_game_score(score_data)

        return LogoutResponse(success=result["success"])

    except Exception as e:
        logger.error(f"Logout error: {e}")
        return LogoutResponse(success=False, error=str(e))


# ============= HEALTH CHECK =============
@app.get("/health")
async def health():
    """Health check endpoint"""
    stats = game_manager.get_stats()
    return {
        "status": "ok",
        "game": GAME_NAME,
        "stats": stats
    }


@app.get("/scores")
async def get_scores(since: str = "2000-01-01T00:00:00"):
    """Return scores recorded after `since` timestamp. Used by RFID poller."""
    try:
        rows = db.get_scores_since(since)
        return {"success": True, "game": GAME_NAME, "scores": rows}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============= GAME SETTINGS ENDPOINT =============
@app.get("/game-settings")
async def get_game_settings():
    """Load game settings from setting folder"""
    import shelve

    try:
        setting_path = str(GAMES_ROOT / "setting" / "led_parameter")
        db = shelve.open(setting_path)

        vh = db.get("value_high")
        vw = db.get("value_width")
        grid_dims = {"rows": int(float(vh or 1)), "cols": int(float(vw or 6))}

        settings = {
            "success": True,
            "wall_layout": str(db.get("wall_light_layout_real") or "")[:100],
            "grid_dims": grid_dims,
            "timeout_seconds": int(float(db.get("game_time_sw") or 5) * 60),
            "max_score": 1000
        }

        db.close()
        return settings
    except Exception as e:
        logger.warning(f"Could not load settings: {e}, using defaults")
        return {
            "success": True,
            "wall_layout": "16x26",
            "grid_dims": {"rows": 16, "cols": 26},
            "timeout_seconds": 180,
            "max_score": 1000
        }


# ============= RUNTIME SETTINGS OVERRIDE (pushed from central RFID server) =====
@app.post("/settings")
async def update_settings(body: dict):
    """Runtime overrides pushed from the central RFID server: default
    difficulty and session length. Written to a small local JSON file,
    read at marathon-start time -- does not touch the original shelve
    config (which stays read-only, ported from the decompiled source)."""
    path = str(GAMES_ROOT / "setting" / "runtime_overrides.json")
    overrides = {}
    if body.get("default_difficulty"):
        overrides["default_difficulty"] = body["default_difficulty"]
    if body.get("session_minutes"):
        overrides["session_minutes"] = int(body["session_minutes"])
    with open(path, "w") as f:
        json.dump(overrides, f)
    return {"success": True, "overrides": overrides}


@app.get("/settings")
async def get_settings():
    path = GAMES_ROOT / "setting" / "runtime_overrides.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


# ============= GAME LEVELS ENDPOINT =============
@app.get("/levels")
async def get_levels():
    """Hoops levels grouped by series.

    Categories:
      casual   - source/-/*.led    (001-011, 1P)
      level    - source/--/*.led   (14-25, 1P)
      dk       - source/---/*.ledb (DK01-DK10, 2P)
    """
    import glob as _glob

    src = str(GAMES_ROOT)

    BUCKETS = [
        ("casual", os.path.join(src, "source", "-",   "*.led"),  False, "led"),
        ("level",  os.path.join(src, "source", "--",  "*.led"),  False, "led"),
        ("dk",     os.path.join(src, "source", "---", "*.ledb"), True,  "ledb"),
    ]

    levels = []
    for bucket, pattern, multiplayer, ftype in BUCKETS:
        for f in sorted(_glob.glob(pattern)):
            stem = os.path.basename(f).rsplit(".", 1)[0]
            display = stem if stem.upper().startswith("DK") else (
                f"Level {int(stem)}" if stem.isdigit() else stem
            )
            levels.append({
                "id":          stem,
                "name":        display,
                "path":        f,
                "category":    bucket,
                "multiplayer": multiplayer,
                "file_type":   ftype,
            })

    by_cat = {}
    for lv in levels:
        by_cat.setdefault(lv["category"], []).append(lv)

    return {
        "success":    True,
        "levels":     levels,
        "categories": by_cat,
        "count":      len(levels),
    }


# ============= GAME INPUT ENDPOINT =============
@app.post("/game-input")
async def game_input(payload: dict):
    """Player input from simulator: press/release a tile.
    Body: {row, col, type: 'press'|'release', game_id?(optional)}.
    Applies to specified game, or first active game if id omitted."""
    row = payload.get("row")
    col = payload.get("col")
    action = payload.get("type", "press")
    game_id = payload.get("game_id")

    if row is None or col is None:
        return {"success": False, "error": "row and col required"}

    game = None
    if game_id:
        game = game_manager.get_game(game_id)
    else:
        for _gid, g in game_manager.games.items():
            game = g
            break

    if not game:
        return {"success": False, "error": "No active game"}

    ok = game.apply_input(int(row), int(col), action)
    return {"success": ok, "score": game.score}


# ============= SAVE SCORE =============
@app.post("/save-score")
async def save_score(payload: dict):
    """Persist a finished game to the leaderboard.
    Body: {card_id, level, score, life, time_used}."""
    try:
        ok = db.record_game_score({
            "card_id": payload.get("card_id", ""),
            "card_id2": payload.get("card_id2", ""),
            "multiplayer": payload.get("multiplayer", False),
            "level": payload.get("level", ""),          # starting level picked
            "end_level": payload.get("end_level", ""),  # level session ended on
            "score": payload.get("score", 0),           # raw P1
            "score2": payload.get("score2", 0),         # raw P2
            "final_score": payload.get("final_score", 0.0),    # normalized P1
            "final_score2": payload.get("final_score2", 0.0),  # normalized P2
            "life": payload.get("life", 0),
            "lives_start": payload.get("lives_start", 0),
            "result": payload.get("result"),
            "time_used": payload.get("time_used", 0.0),  # full session duration
            "levels_cleared": payload.get("levels_cleared", 0),
            "difficulty": payload.get("difficulty", ""),
            "started_at": payload.get("started_at", ""),
        })
        return {"success": ok}
    except Exception as e:
        logger.error(f"save-score error: {e}")
        return {"success": False, "error": str(e)}


# ============= LEADERBOARD =============
@app.get("/leaderboard/{level}")
async def leaderboard(level: str, limit: int = 10):
    """Top scores for a level."""
    return {"success": True, "level": level,
            "entries": db.get_leaderboard(level=level, limit=limit)}


# ============= ACTIVE GAME (resume on reload) =============
@app.get("/active-game")
async def active_game():
    """Return the currently-running game (if any) with its full config so the
    frontend can resume the simulator after a page reload instead of starting
    over at login."""
    for gid, g in game_manager.games.items():
        if g.running and not g.get_state().get("game_over"):
            return {
                "success": True,
                "game_id": gid,
                "card_id": g.card_id,
                "level": g.level,
                "difficulty": g.difficulty,
                "player_count": g.session_player_count,
                "state": g.get_state(),
            }
    return {"success": False}


# ============= GAME STATE BY ID =============
@app.get("/game-state/{game_id}")
async def get_game_state_by_id(game_id: str):
    """Get state of a SPECIFIC game (simulator polls its own game)."""
    game = game_manager.get_game(game_id)
    if not game:
        return {"success": False, "error": "Game not found"}
    return {"success": True, "game_id": game_id, "state": game.get_state()}


# ============= GAME STATE ENDPOINT =============
@app.get("/game-state")
async def get_game_state():
    """Get current active game state (for simulator)"""
    stats = game_manager.get_stats()

    if stats["active_games"] == 0:
        return {"success": False, "error": "No active games"}

    # Get first active game
    for game_id, game in game_manager.games.items():
        return {
            "success": True,
            "game_id": game_id,
            "state": game.get_state()
        }

    return {"success": False, "error": "No games found"}


# ============= STARTUP/SHUTDOWN =============
@app.on_event("startup")
async def startup():
    logger.info(f"API starting on {API_HOST}:{API_PORT}")
    logger.info(f"Game: {GAME_NAME}")


@app.get("/hw-debug")
async def hw_debug():
    """Live hardware loop diagnostics."""
    import os as _os
    games = []
    for gid, g in game_manager.games.items():
        games.append({
            "game_id": gid,
            "running": g.running,
            "score": getattr(g, "score", 0),
            "hw_draw_count": getattr(g, "_hw_draw_count", 0),
            "last_hw_draw": getattr(g, "_hw_last_draw", 0),
        })
    return {
        "use_serial_hd": _os.environ.get("USE_SERIAL_HD", "0") == "1",
        "active_games": len(games),
        "games": games,
        "zombie_threads": game_manager.zombie_threads,
    }


@app.on_event("shutdown")
async def shutdown():
    logger.info("API shutting down")
    db.close()


# ============= RUN =============
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=API_HOST,
        port=API_PORT,
        log_level="info"
    )
