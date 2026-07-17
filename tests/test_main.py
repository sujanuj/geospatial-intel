"""Tests for main.py.

Covers REST endpoints, WebSocket connect/init/geofence-ack behavior, and a
regression test for the UnboundLocalError bug in broadcast_updates() (see
README, "Bugs found & fixed", #2).

Note on test isolation: main.py holds simulator and connected_clients as
module-level singletons (not dependency-injected), matching the app's
actual runtime design. That means these tests share state across a test
run, same as the real app does across requests — this is intentional
fidelity to production behavior, not an oversight. Tests that need a clean
slate reset the relevant global explicitly.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

class TestRestEndpoints:
    def test_get_objects_returns_all_35(self, client):
        resp = client.get("/api/objects")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["objects"]) == 35
        assert body["stats"]["total"] == 35

    def test_get_stats_shape(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("total", "ships", "aircraft", "vehicles", "active", "warning", "threat"):
            assert key in body

    def test_get_single_object_found(self, client):
        obj_id = main.simulator.get_all()[0]["id"]
        resp = client.get(f"/api/objects/{obj_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == obj_id

    def test_get_single_object_not_found_returns_404(self, client):
        resp = client.get("/api/objects/does-not-exist")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_root_serves_frontend_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "GEO-INTEL" in resp.text or "geo-intel" in resp.text.lower()


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

class TestWebSocket:
    def test_receives_init_message_on_connect(self, client):
        with client.websocket_connect("/ws") as ws:
            data = json.loads(ws.receive_text())
            assert data["type"] == "init"
            assert len(data["objects"]) == 35
            assert "stats" in data
            assert "timestamp" in data

    def test_geofence_message_gets_ack(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # discard init message
            ws.send_text(json.dumps({"type": "geofence", "bounds": [0, 0, 1, 1]}))
            data = json.loads(ws.receive_text())
            assert data["type"] == "geofence_ack"

    def test_connecting_adds_to_connected_clients(self, client):
        main.connected_clients.clear()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # init
            assert len(main.connected_clients) == 1
        # after the `with` block exits, the client has disconnected;
        # connected_clients cleanup happens in the except block on the
        # server side, which we verify separately in TestBroadcastUpdates


# ---------------------------------------------------------------------------
# broadcast_updates() — regression tests for the UnboundLocalError bug
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Minimal stand-in for a WebSocket connection. broadcast_updates()
    only ever calls .send_text() on entries in connected_clients, so that's
    all this needs to implement."""

    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.sent = []

    async def send_text(self, message: str):
        if self.should_fail:
            raise ConnectionError("simulated dead connection")
        self.sent.append(message)


class TestBroadcastUpdates:
    """Regression coverage for bug #2 in the README: `connected_clients -=
    dead` rebinding the name and causing an UnboundLocalError on the
    earlier `if connected_clients:` read. The fix was
    `connected_clients.difference_update(dead)`, mutating in place."""

    @pytest.mark.asyncio
    async def test_broadcast_runs_without_unbound_local_error(self):
        main.connected_clients.clear()
        live = FakeWebSocket(should_fail=False)
        dead = FakeWebSocket(should_fail=True)
        main.connected_clients.add(live)
        main.connected_clients.add(dead)

        task = asyncio.create_task(main.broadcast_updates())
        try:
            # broadcast_updates() sleeps 1.0s before its first send; give it
            # enough margin to complete one full iteration.
            await asyncio.sleep(1.2)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # If the UnboundLocalError bug had regressed, the task would have
        # raised inside the loop and task.exception() would be set instead
        # of the loop simply being cancelled cleanly.
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_dead_socket_removed_live_socket_kept(self):
        main.connected_clients.clear()
        live = FakeWebSocket(should_fail=False)
        dead = FakeWebSocket(should_fail=True)
        main.connected_clients.add(live)
        main.connected_clients.add(dead)

        task = asyncio.create_task(main.broadcast_updates())
        await asyncio.sleep(1.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert live in main.connected_clients
        assert dead not in main.connected_clients
        assert len(live.sent) == 1  # received exactly one broadcast

    @pytest.mark.asyncio
    async def test_broadcast_with_zero_clients_does_not_raise(self):
        """The `if connected_clients:` guard should make an empty set a
        no-op, not a crash — this is the exact line that broke when
        connected_clients was accidentally shadowed as a local."""
        main.connected_clients.clear()
        task = asyncio.create_task(main.broadcast_updates())
        await asyncio.sleep(1.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled()
