"""FastAPI backend for the geospatial intelligence platform.

Endpoints:
  GET  /              -> serve the frontend HTML
  GET  /api/objects   -> get all objects (REST); accepts ?q=<query> to filter
  GET  /api/objects/{id} -> get single object
  GET  /api/stats     -> get fleet statistics
  GET  /api/geofences -> list active geofences
  POST /api/geofences -> create a geofence
  DELETE /api/geofences/{id} -> remove a geofence
  GET  /api/alerts    -> recent geofence enter/exit alerts (most recent first)
  WS   /ws            -> WebSocket for real-time position updates

The WebSocket broadcasts all object positions every second to all
connected clients. This is how real C2 (command and control) systems
push situational awareness updates to operators.

Objects can be filtered with a small query DSL (see query.py), e.g.
`type:ship AND status:threat`. It applies to GET /api/objects via a `q`
query parameter, and per-connection on the WebSocket via a
`{"type": "filter", "query": "..."}` message -- each connected client can
have a different active filter at the same time.

Geofences (see geofence.py) are circular zones that trigger an alert the
moment an object enters or exits. Unlike query filters, geofences are
global, not per-connection -- creating or deleting one broadcasts to every
connected client, since a geofence represents a real zone of interest that
everyone watching the map should see and be alerted about.
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from simulator import ObjectSimulator
from query import QueryError, filter_objects
from geofence import GeofenceError, GeofenceManager

# Global simulator instance
simulator = ObjectSimulator(n_ships=15, n_aircraft=10, n_vehicles=10)

# Global geofence manager. Geofences are shared across all connections
# (unlike query filters, which are per-connection) -- see module docstring.
geofence_manager = GeofenceManager()

# Rolling log of the most recent geofence alerts, newest first. Capped so
# a long-running process doesn't grow this unboundedly; a real system
# would persist these somewhere instead of keeping them only in memory.
ALERT_LOG_MAX = 200
alert_log: List[dict] = []

# Track connected WebSocket clients
connected_clients: Set[WebSocket] = set()

# Per-connection query filter. A client with no entry (or a None value)
# receives the unfiltered object stream. Setting a filter via a "filter"
# WebSocket message narrows what that specific connection receives on
# every subsequent broadcast tick -- each client can have a different
# filter active at the same time.
client_filters: Dict[WebSocket, Optional[str]] = {}


class GeofenceCreate(BaseModel):
    name: str
    lat: float
    lon: float
    radius_km: float


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

    Geofence alerts are computed once per tick against the *unfiltered*
    object list (an object shouldn't need to match someone's personal
    query filter to trigger a geofence alert -- the two features are
    independent) and included in every client's message, filtered or not.
    """
    while True:
        await asyncio.sleep(1.0)
        simulator.update(dt=1.0)

        all_objects = simulator.get_all()
        new_alerts = geofence_manager.update(all_objects)
        if new_alerts:
            alert_log[:0] = reversed(new_alerts)  # newest first
            del alert_log[ALERT_LOG_MAX:]

        if not connected_clients:
            continue

        stats = simulator.get_stats()
        timestamp = time.time()

        # Precompute the unfiltered message once; most connections won't
        # have an active filter, so this avoids re-serializing JSON for
        # every client on every tick.
        unfiltered_message = json.dumps({
            "type": "update",
            "objects": all_objects,
            "stats": stats,
            "alerts": new_alerts,
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
                    "alerts": new_alerts,
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


async def broadcast_to_all(message: dict):
    """Send a message to every connected client immediately, outside the
    normal per-tick loop. Used for geofence create/delete, since those are
    global events every connected client should see right away, not wait
    up to a second for."""
    if not connected_clients:
        return
    text = json.dumps(message)
    dead = set()
    for ws in connected_clients:
        try:
            await ws.send_text(text)
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
# Geofence endpoints
# ---------------------------------------------------------------------------

@app.get("/api/geofences")
async def list_geofences():
    return JSONResponse({"geofences": geofence_manager.list()})


@app.post("/api/geofences")
async def create_geofence(body: GeofenceCreate):
    try:
        gf = geofence_manager.create(body.name, body.lat, body.lon, body.radius_km)
    except GeofenceError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    gf_dict = gf.to_dict()
    # Every connected client should see the new zone immediately, not wait
    # for the next broadcast tick.
    await broadcast_to_all({"type": "geofence_created", "geofence": gf_dict})
    return JSONResponse(gf_dict, status_code=201)


@app.delete("/api/geofences/{geofence_id}")
async def delete_geofence(geofence_id: str):
    removed = geofence_manager.delete(geofence_id)
    if not removed:
        return JSONResponse({"error": "Geofence not found"}, status_code=404)
    await broadcast_to_all({"type": "geofence_deleted", "id": geofence_id})
    return JSONResponse({"deleted": geofence_id})


@app.get("/api/alerts")
async def get_alerts(limit: int = 50):
    return JSONResponse({"alerts": alert_log[:limit], "total_logged": len(alert_log)})


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
        "geofences": geofence_manager.list(),
        "timestamp": time.time(),
    }))

    try:
        while True:
            # Keep connection alive, handle client messages
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")

            # Create, delete, or list geofences. Unlike query filters,
            # geofences are global -- a successful create/delete is
            # broadcast to every connected client via broadcast_to_all(),
            # not just acked back to the requester.
            if msg_type == "geofence":
                action = msg.get("action")

                if action == "create":
                    try:
                        gf = geofence_manager.create(
                            msg.get("name", ""),
                            float(msg.get("lat")),
                            float(msg.get("lon")),
                            float(msg.get("radius_km")),
                        )
                    except (GeofenceError, TypeError, ValueError) as e:
                        await websocket.send_text(json.dumps({
                            "type": "geofence_error",
                            "error": str(e),
                        }))
                    else:
                        await broadcast_to_all({
                            "type": "geofence_created",
                            "geofence": gf.to_dict(),
                        })

                elif action == "delete":
                    geofence_id = msg.get("id", "")
                    removed = geofence_manager.delete(geofence_id)
                    if removed:
                        await broadcast_to_all({
                            "type": "geofence_deleted",
                            "id": geofence_id,
                        })
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "geofence_error",
                            "error": f"geofence {geofence_id!r} not found",
                        }))

                elif action == "list":
                    await websocket.send_text(json.dumps({
                        "type": "geofence_list",
                        "geofences": geofence_manager.list(),
                    }))

                else:
                    await websocket.send_text(json.dumps({
                        "type": "geofence_error",
                        "error": f"unknown geofence action {action!r} -- "
                                 "expected create, delete, or list",
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
