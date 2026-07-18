"""FastAPI backend for the geospatial intelligence platform.

Endpoints:
  GET  /              -> serve the frontend HTML
  GET  /api/objects   -> get all objects (REST); accepts ?q=<query> to filter
  GET  /api/objects/{id} -> get single object
  GET  /api/stats     -> get fleet statistics
  WS   /ws            -> WebSocket for real-time position updates

The WebSocket broadcasts all object positions every second to all
connected clients. This is how real C2 (command and control) systems
push situational awareness updates to operators.

Objects can be filtered with a small query DSL (see query.py), e.g.
`type:ship AND status:threat`. It applies to GET /api/objects via a `q`
query parameter, and per-connection on the WebSocket via a
`{"type": "filter", "query": "..."}` message -- each connected client can
have a different active filter at the same time.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from simulator import ObjectSimulator
from query import QueryError, filter_objects

# Global simulator instance
simulator = ObjectSimulator(n_ships=15, n_aircraft=10, n_vehicles=10)

# Track connected WebSocket clients
connected_clients: Set[WebSocket] = set()

# Per-connection query filter. A client with no entry (or a None value)
# receives the unfiltered object stream. Setting a filter via a "filter"
# WebSocket message narrows what that specific connection receives on
# every subsequent broadcast tick -- each client can have a different
# filter active at the same time.
client_filters: Dict[WebSocket, Optional[str]] = {}


# ---------------------------------------------------------------------------
# Background task: update simulator and broadcast
# ---------------------------------------------------------------------------

async def broadcast_updates():
    """Update all object positions every second and broadcast to clients.

    Each connection can have its own active query filter (see the "filter"
    WebSocket message below), so this can't always send one identical
    message to every client -- a client with no filter gets the full
    object list; a client with a filter gets only the matching subset,
    computed fresh each tick since object state (position, status) changes
    every tick too.
    """
    while True:
        await asyncio.sleep(1.0)
        simulator.update(dt=1.0)

        if not connected_clients:
            continue

        all_objects = simulator.get_all()
        stats = simulator.get_stats()
        timestamp = time.time()

        # Precompute the unfiltered message once; most connections won't
        # have an active filter, so this avoids re-serializing JSON for
        # every client on every tick.
        unfiltered_message = json.dumps({
            "type": "update",
            "objects": all_objects,
            "stats": stats,
            "timestamp": timestamp,
        })

        dead = set()
        for ws in connected_clients:
            query = client_filters.get(ws)
            if query:
                try:
                    matched = filter_objects(all_objects, query)
                except QueryError:
                    # The filter was validated when the client set it, so
                    # this shouldn't happen in practice -- but object dicts
                    # could in principle drift from what the filter expects
                    # (e.g. a field renamed in simulator.py). Fail safe by
                    # falling back to the unfiltered view for this tick
                    # rather than silently dropping the client.
                    matched = all_objects
                message = json.dumps({
                    "type": "update",
                    "objects": matched,
                    "stats": stats,
                    "matched": len(matched),
                    "timestamp": timestamp,
                })
            else:
                message = unfiltered_message

            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)

        connected_clients.difference_update(dead)
        for ws in dead:
            client_filters.pop(ws, None)


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
async def get_objects(q: Optional[str] = None):
    """List all objects, optionally filtered by a query string.

    Example: GET /api/objects?q=type:ship%20AND%20status:threat
    """
    all_objects = simulator.get_all()
    if q:
        try:
            matched = filter_objects(all_objects, q)
        except QueryError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({
            "objects": matched,
            "stats": simulator.get_stats(),
            "query": q,
            "matched": len(matched),
        })
    return JSONResponse({"objects": all_objects, "stats": simulator.get_stats()})


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
    client_filters[websocket] = None

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
            msg_type = msg.get("type")

            # Handle geofence alerts
            if msg_type == "geofence":
                await websocket.send_text(json.dumps({
                    "type": "geofence_ack",
                    "message": "Geofence registered",
                }))

            # Set (or clear, if query is empty/absent) this connection's
            # active filter. Validated immediately against the current
            # object list so the client gets a fast, specific error instead
            # of silently filtering nothing on the next broadcast tick.
            elif msg_type == "filter":
                query = msg.get("query", "")
                if not query:
                    client_filters[websocket] = None
                    await websocket.send_text(json.dumps({
                        "type": "filter_ack",
                        "query": None,
                        "matched": len(simulator.get_all()),
                    }))
                else:
                    try:
                        matched = filter_objects(simulator.get_all(), query)
                    except QueryError as e:
                        await websocket.send_text(json.dumps({
                            "type": "filter_error",
                            "query": query,
                            "error": str(e),
                        }))
                    else:
                        client_filters[websocket] = query
                        await websocket.send_text(json.dumps({
                            "type": "filter_ack",
                            "query": query,
                            "matched": len(matched),
                        }))

            elif msg_type == "clear_filter":
                client_filters[websocket] = None
                await websocket.send_text(json.dumps({
                    "type": "filter_ack",
                    "query": None,
                    "matched": len(simulator.get_all()),
                }))
    except WebSocketDisconnect:
        connected_clients.discard(websocket)
        client_filters.pop(websocket, None)
    except Exception:
        connected_clients.discard(websocket)
        client_filters.pop(websocket, None)


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    html_path = Path("frontend/index.html")
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Frontend not found. Run from project root.</h1>")
