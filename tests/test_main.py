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


class TestRestQueryFiltering:
    """GET /api/objects?q=<query> — see query.py for the DSL itself."""

    def test_filters_by_type(self, client):
        resp = client.get("/api/objects", params={"q": "type:ship"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched"] == len(body["objects"])
        assert all(o["type"] == "ship" for o in body["objects"])
        assert body["matched"] == 15  # simulator is configured with 15 ships

    def test_filters_by_compound_query(self, client):
        resp = client.get("/api/objects", params={"q": "type:aircraft AND speed>300"})
        assert resp.status_code == 200
        body = resp.json()
        assert all(o["type"] == "aircraft" and o["speed"] > 300 for o in body["objects"])

    def test_malformed_query_returns_400_with_message(self, client):
        resp = client.get("/api/objects", params={"q": "bogus_field:value"})
        assert resp.status_code == 400
        assert "unknown field" in resp.json()["error"]

    def test_no_query_param_returns_unfiltered_shape(self, client):
        # response shape without ?q= should NOT include "matched"/"query" —
        # confirms the two code paths (filtered vs unfiltered) stay distinct
        resp = client.get("/api/objects")
        body = resp.json()
        assert "matched" not in body
        assert "query" not in body
        assert len(body["objects"]) == 35


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

    def test_geofence_create_via_websocket(self, client):
        main.geofence_manager.geofences.clear()
        main.geofence_manager._containment.clear()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # discard init message
            ws.send_text(json.dumps({
                "type": "geofence", "action": "create",
                "name": "Zone A", "lat": 25.0, "lon": 55.0, "radius_km": 100,
            }))
            data = json.loads(ws.receive_text())
            assert data["type"] == "geofence_created"
            assert data["geofence"]["name"] == "Zone A"

    def test_geofence_create_with_invalid_params_errors(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # discard init
            ws.send_text(json.dumps({
                "type": "geofence", "action": "create",
                "name": "", "lat": 25.0, "lon": 55.0, "radius_km": 100,
            }))
            data = json.loads(ws.receive_text())
            assert data["type"] == "geofence_error"

    def test_geofence_unknown_action_errors(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # discard init
            ws.send_text(json.dumps({"type": "geofence", "action": "bogus"}))
            data = json.loads(ws.receive_text())
            assert data["type"] == "geofence_error"
            assert "unknown geofence action" in data["error"]

    def test_geofence_missing_action_errors(self, client):
        """The pre-DSL stub used to ack any {"type": "geofence"} message
        regardless of shape; now that geofences are a real feature, a
        message with no action is a client error, not a silent no-op."""
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # discard init
            ws.send_text(json.dumps({"type": "geofence", "bounds": [0, 0, 1, 1]}))
            data = json.loads(ws.receive_text())
            assert data["type"] == "geofence_error"

    def test_connecting_adds_to_connected_clients(self, client):
        main.connected_clients.clear()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # init
            assert len(main.connected_clients) == 1
        # after the `with` block exits, the client has disconnected;
        # connected_clients cleanup happens in the except block on the
        # server side, which we verify separately in TestBroadcastUpdates


class TestWebSocketFiltering:
    """The {"type": "filter", "query": "..."} / clear_filter messages and
    their effect on connected_clients / client_filters bookkeeping."""

    def test_filter_ack_on_valid_query(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # discard init
            ws.send_text(json.dumps({"type": "filter", "query": "type:ship"}))
            data = json.loads(ws.receive_text())
            assert data["type"] == "filter_ack"
            assert data["query"] == "type:ship"
            assert data["matched"] == 15

    def test_filter_error_on_malformed_query(self, client):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # discard init
            ws.send_text(json.dumps({"type": "filter", "query": "bogus:field:oops"}))
            data = json.loads(ws.receive_text())
            assert data["type"] == "filter_error"
            assert data["query"] == "bogus:field:oops"
            assert "error" in data

    def test_setting_filter_updates_client_filters_dict(self, client):
        main.connected_clients.clear()
        main.client_filters.clear()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # init
            ws_obj = list(main.connected_clients)[0]
            assert main.client_filters[ws_obj] is None  # no filter yet

            ws.send_text(json.dumps({"type": "filter", "query": "type:aircraft"}))
            ws.receive_text()  # filter_ack
            assert main.client_filters[ws_obj] == "type:aircraft"

    def test_clear_filter_resets_to_none(self, client):
        main.connected_clients.clear()
        main.client_filters.clear()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # init
            ws_obj = list(main.connected_clients)[0]

            ws.send_text(json.dumps({"type": "filter", "query": "type:ship"}))
            ws.receive_text()  # filter_ack
            assert main.client_filters[ws_obj] == "type:ship"

            ws.send_text(json.dumps({"type": "clear_filter"}))
            data = json.loads(ws.receive_text())
            assert data["type"] == "filter_ack"
            assert data["query"] is None
            assert main.client_filters[ws_obj] is None

    def test_invalid_filter_does_not_overwrite_previously_valid_one(self, client):
        """A filter_error response shouldn't silently clear or corrupt a
        filter that was already successfully applied."""
        main.connected_clients.clear()
        main.client_filters.clear()
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()  # init
            ws_obj = list(main.connected_clients)[0]

            ws.send_text(json.dumps({"type": "filter", "query": "type:ship"}))
            ws.receive_text()  # filter_ack
            assert main.client_filters[ws_obj] == "type:ship"

            ws.send_text(json.dumps({"type": "filter", "query": "not valid(("}))
            data = json.loads(ws.receive_text())
            assert data["type"] == "filter_error"
            assert main.client_filters[ws_obj] == "type:ship"  # unchanged


class TestBroadcastFiltering:
    """broadcast_updates() actually applying per-client filters, using the
    same FakeWebSocket approach as TestBroadcastUpdates."""

    @pytest.mark.asyncio
    async def test_filtered_client_receives_only_matching_objects(self):
        main.connected_clients.clear()
        main.client_filters.clear()
        filtered_ws = FakeWebSocket(should_fail=False)
        unfiltered_ws = FakeWebSocket(should_fail=False)
        main.connected_clients.add(filtered_ws)
        main.connected_clients.add(unfiltered_ws)
        main.client_filters[filtered_ws] = "type:ship"
        main.client_filters[unfiltered_ws] = None

        task = asyncio.create_task(main.broadcast_updates())
        await asyncio.sleep(1.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(filtered_ws.sent) == 1
        assert len(unfiltered_ws.sent) == 1

        filtered_msg = json.loads(filtered_ws.sent[0])
        unfiltered_msg = json.loads(unfiltered_ws.sent[0])

        assert all(o["type"] == "ship" for o in filtered_msg["objects"])
        assert filtered_msg["matched"] == len(filtered_msg["objects"])
        assert len(unfiltered_msg["objects"]) == 35  # unfiltered client sees everything

        main.client_filters.clear()


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


class TestGeofenceRestEndpoints:
    def setup_method(self):
        main.geofence_manager.geofences.clear()
        main.geofence_manager._containment.clear()

    def test_create_geofence(self, client):
        resp = client.post("/api/geofences", json={
            "name": "Zone A", "lat": 25.0, "lon": 55.0, "radius_km": 100,
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Zone A"
        assert "id" in body

    def test_create_geofence_invalid_params_returns_400(self, client):
        resp = client.post("/api/geofences", json={
            "name": "Zone A", "lat": 999.0, "lon": 55.0, "radius_km": 100,
        })
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_list_geofences(self, client):
        client.post("/api/geofences", json={
            "name": "Zone A", "lat": 25.0, "lon": 55.0, "radius_km": 100,
        })
        resp = client.get("/api/geofences")
        assert resp.status_code == 200
        assert len(resp.json()["geofences"]) == 1

    def test_delete_geofence(self, client):
        created = client.post("/api/geofences", json={
            "name": "Zone A", "lat": 25.0, "lon": 55.0, "radius_km": 100,
        }).json()
        resp = client.delete(f"/api/geofences/{created['id']}")
        assert resp.status_code == 200
        assert client.get("/api/geofences").json()["geofences"] == []

    def test_delete_unknown_geofence_returns_404(self, client):
        resp = client.delete("/api/geofences/does-not-exist")
        assert resp.status_code == 404

    def test_get_alerts_empty_initially(self, client):
        main.alert_log.clear()
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        assert resp.json()["alerts"] == []


class TestGeofenceAlertsInBroadcast:
    """Confirms geofence enter/exit alerts actually flow through the
    broadcast loop into client messages, and into the alert log."""

    @pytest.mark.asyncio
    async def test_object_entering_geofence_produces_alert_in_broadcast(self):
        main.geofence_manager.geofences.clear()
        main.geofence_manager._containment.clear()
        main.alert_log.clear()
        main.connected_clients.clear()
        main.client_filters.clear()

        # Place the geofence directly on top of a real object's current
        # position, so it's guaranteed to be "inside" on the very next tick.
        target = main.simulator.get_all()[0]
        main.geofence_manager.create(
            "Test Zone", lat=target["lat"], lon=target["lon"], radius_km=5000
        )

        ws = FakeWebSocket(should_fail=False)
        main.connected_clients.add(ws)
        main.client_filters[ws] = None

        task = asyncio.create_task(main.broadcast_updates())
        await asyncio.sleep(1.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(ws.sent) == 1
        msg = json.loads(ws.sent[0])
        assert "alerts" in msg
        assert len(msg["alerts"]) >= 1
        assert msg["alerts"][0]["event"] == "enter"

        assert len(main.alert_log) >= 1
        assert main.alert_log[0]["event"] == "enter"
