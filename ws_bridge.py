"""
WebSocket bridge: headless API game state → simulator UI
Connects to http://localhost:8000 API, broadcasts game state via WebSocket
"""

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import httpx

app = FastAPI()

# Serve static simulator files
SIMULATOR_STATIC = str(Path(__file__).resolve().parent / "simulator" / "static")
app.mount("/static", StaticFiles(directory=SIMULATOR_STATIC), name="static")

HOST = "127.0.0.1"
_DEFAULT_API_PORT = 8000
_DEFAULT_WS_PORT = 8765
API_PORT = int(os.getenv("API_PORT", _DEFAULT_API_PORT))
PORT = int(os.getenv("WS_BRIDGE_PORT", _DEFAULT_WS_PORT))
API_BASE_URL = os.getenv("API_BASE_URL", f"http://localhost:{API_PORT}")

class GameBridge:
    """Bridge between API and WebSocket clients"""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.current_game_id = None
        self.game_state = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.active_connections.add(ws)
        print(f"[OK] Client connected. Total: {len(self.active_connections)}")

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            self.active_connections.discard(ws)
        print(f"[OK] Client disconnected. Total: {len(self.active_connections)}")

    async def broadcast_state(self, game_state: dict):
        """Send game state to clients. Climb = SQUARE grid, SINGLE RGB per cell."""
        if not self.active_connections:
            return

        # Hoops: led_display is flat list of [R,G,B] per hoop column.
        # Grid dims come from the API (1×N strip); default 1×6.
        led_display = game_state.get("led_display", [])
        rows = int(game_state.get("grid_rows", 1))
        cols = int(game_state.get("grid_cols", 6))

        def _rgb(cell):
            if isinstance(cell, (list, tuple)) and len(cell) >= 3:
                # single [r,g,b]
                if not isinstance(cell[0], (list, tuple)):
                    return [int(cell[0]), int(cell[1]), int(cell[2])]
                # legacy 3-ring -> take middle ring
                mid = cell[1] if len(cell) > 1 else cell[0]
                return [int(mid[0]), int(mid[1]), int(mid[2])]
            return [0, 0, 0]

        grid = []
        if led_display:
            n = len(led_display)
            if rows * cols != n:
                # Hoops 1×N strip: infer cols from buffer when level/grid disagree.
                if rows == 1:
                    cols = n
                elif n % rows == 0:
                    cols = n // rows
            if n == rows * cols:
                for i in range(rows):
                    grid.append([_rgb(led_display[i * cols + j]) for j in range(cols)])
            else:
                grid = [[[0, 0, 0] for _ in range(cols)] for _ in range(rows)]
        else:
            grid = [[[0, 0, 0] for _ in range(cols)] for _ in range(rows)]

        msg = json.dumps({
            "type": "frame",
            "rows": rows,
            "cols": cols,
            "grid": grid,
            "pressed": game_state.get("pressed_tiles", []),
            "fps": 60,
            "game_id": self.current_game_id
        })

        async with self.lock:
            conns = list(self.active_connections)

        async def _send(ws):
            try:
                await ws.send_text(msg)
                return None
            except Exception as e:
                print(f"[ERR] Send error: {e}")
                return ws

        results = await asyncio.gather(*(_send(ws) for ws in conns), return_exceptions=False)
        dead = {ws for ws in results if ws is not None}
        if dead:
            async with self.lock:
                self.active_connections -= dead

bridge = GameBridge()

@app.get("/")
async def index():
    """Serve simulator UI"""
    return FileResponse(f"{SIMULATOR_STATIC}/index.html")

@app.get("/status")
async def status():
    """Health check"""
    return {
        "status": "ok",
        "game_id": bridge.current_game_id,
        "game_state": bridge.game_state,
        "connected_clients": len(bridge.active_connections)
    }

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint for simulator UI"""
    client_game_id = ws.query_params.get("game_id")
    await bridge.connect(ws)
    async with httpx.AsyncClient() as client:
        try:
            while True:
                # Client sends tile commands: {type:'press'|'release', row, col}
                msg = await ws.receive_text()
                try:
                    data = json.loads(msg)
                    if data.get("type") in ("press", "release"):
                        gid = client_game_id or bridge.current_game_id
                        # Forward to API game-input endpoint
                        await client.post(
                            f"{API_BASE_URL}/game-input",
                            json={
                                "row": data.get("row"),
                                "col": data.get("col"),
                                "type": data["type"],
                                "game_id": gid,
                            },
                            timeout=2,
                        )
                except Exception as e:
                    print(f"[ERR] Input forward error: {e}")
        except WebSocketDisconnect:
            await bridge.disconnect(ws)

async def poll_game_state():
    """Poll API for game state and broadcast to clients"""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Get active game state from API
                resp = await client.get(f"{API_BASE_URL}/active-game", timeout=5)
                data = resp.json()

                if data.get("success"):
                    # Game running - update state
                    bridge.current_game_id = data["game_id"]
                    bridge.game_state = data.get("state", {})
                    await bridge.broadcast_state(bridge.game_state)
                else:
                    # No active game — send blank frame so simulator clears
                    bridge.current_game_id = None
                    bridge.game_state = {}
                    await bridge.broadcast_state({
                        "led_display": [[0, 0, 0]] * 6,
                        "grid_rows": 1,
                        "grid_cols": 6,
                    })

                await asyncio.sleep(0.033)  # ~30fps

            except Exception as e:
                print(f"[ERR] Poll error: {e!r}")
                await asyncio.sleep(1)

@app.on_event("startup")
async def _start_poller():
    """Run poller in uvicorn's own event loop (same loop as websockets).
    Avoids cross-loop 'bound to a different event loop' errors."""
    asyncio.create_task(poll_game_state())

if __name__ == "__main__":
    print(f"WS Bridge: {HOST}:{PORT}")
    print(f"API: {API_BASE_URL}")
    print(f"Web UI: http://{HOST}:{PORT}")

    # Start WebSocket server (poller launches via startup event)
    # loop="asyncio": uvloop breaks when launched detached/frozen (no TTY signals).
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", loop="asyncio")
