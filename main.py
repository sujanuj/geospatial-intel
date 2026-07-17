"""FastAPI backend for the geospatial intelligence platform.

Endpoints:
  GET  /              -> serve the frontend HTML
  GET  /api/objects   -> get all objects (REST)
  GET  /api/objects/{id} -> get single object
  GET  /api/stats     -> get fleet statistics
  WS   /ws            -> WebSocket for real-time position updates

The WebSocket broadcasts all object positions every second to all
connected clients. This is how real C2 (command and control) systems
push situational awareness updates to operators.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from simulator import ObjectSimulator

# Global simulator instance
simulator = ObjectSimulator(n_ships=15, n_aircraft=10, n_vehicles=10)

# Track connected WebSocket clients
connected_clients: Set[WebSocket] = set()


# ---------------------------------------------------------------------------
# Background task: update simulator and broadcast
# ---------------------------------------------------------------------------

async def broadcast_updates():
    """Update all object positions every second and broadcast to clients."""
    while True:
        await asyncio.sleep(1.0)
        simulator.update(dt=1.0)

        if connected_clients:
            message = json.dumps({
                "type": "update",
                "objects": simulator.get_all(),
                "stats": simulator.get_stats(),
                "timestamp": time.time(),
            })
            dead = set()
            for ws in connected_clients:
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.add(ws)
            connected_clients.difference_update(dead)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: kick off the background broadcast loop.
    task = asyncio.create_task(broadcast_updates())
    yield
    # Shutdown: cancel the broadcast loop cleanly instead of letting it get
    # killed mid-iteration when the process exits.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Geospatial Intelligence Platform", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/objects")
async def get_objects():
    return JSONResponse({"objects": simulator.get_all(), "stats": simulator.get_stats()})


@app.get("/api/objects/{obj_id}")
async def get_object(obj_id: str):
    obj = simulator.get_by_id(obj_id)
    if obj is None:
        return JSONResponse({"error": "Object not found"}, status_code=404)
    return JSONResponse(obj)


@app.get("/api/stats")
async def get_stats():
    return JSONResponse(simulator.get_stats())


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)

    # Send initial state immediately
    await websocket.send_text(json.dumps({
        "type": "init",
        "objects": simulator.get_all(),
        "stats": simulator.get_stats(),
        "timestamp": time.time(),
    }))

    try:
        while True:
            # Keep connection alive, handle client messages
            data = await websocket.receive_text()
            msg = json.loads(data)

            # Handle geofence alerts
            if msg.get("type") == "geofence":
                await websocket.send_text(json.dumps({
                    "type": "geofence_ack",
                    "message": "Geofence registered",
                }))
    except WebSocketDisconnect:
        connected_clients.discard(websocket)
    except Exception:
        connected_clients.discard(websocket)


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    html_path = Path("frontend/index.html")
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Frontend not found. Run from project root.</h1>")
