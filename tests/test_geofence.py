"""Tests for geofence.py — haversine distance, geofence CRUD, and
enter/exit transition detection.
"""

import pytest

from geofence import GeofenceError, GeofenceManager, haversine_km


# ---------------------------------------------------------------------------
# haversine_km
# ---------------------------------------------------------------------------

class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_km(25.0, 55.0, 25.0, 55.0) == pytest.approx(0.0, abs=1e-9)

    def test_known_distance_nyc_to_london(self):
        # real-world great-circle distance is ~5570 km
        d = haversine_km(40.7128, -74.0060, 51.5074, -0.1278)
        assert d == pytest.approx(5570, rel=0.01)

    def test_known_distance_sf_to_la(self):
        # real-world great-circle distance is ~560 km
        d = haversine_km(37.7749, -122.4194, 34.0522, -118.2437)
        assert d == pytest.approx(560, rel=0.02)

    def test_symmetric(self):
        d1 = haversine_km(10.0, 20.0, 30.0, 40.0)
        d2 = haversine_km(30.0, 40.0, 10.0, 20.0)
        assert d1 == pytest.approx(d2)

    def test_antimeridian_wraparound(self):
        # two points 2 degrees apart straddling +-180 longitude should be
        # close together, not almost the full circumference apart
        d = haversine_km(0.0, 179.0, 0.0, -179.0)
        assert d == pytest.approx(222, rel=0.02)
        assert d < 1000  # sanity: nowhere near "the long way around"

    def test_one_degree_latitude_is_about_111km(self):
        d = haversine_km(0.0, 0.0, 1.0, 0.0)
        assert d == pytest.approx(111, rel=0.02)


# ---------------------------------------------------------------------------
# Geofence creation / validation
# ---------------------------------------------------------------------------

class TestGeofenceCreation:
    def test_create_returns_geofence_with_id(self):
        mgr = GeofenceManager()
        gf = mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        assert gf.id
        assert gf.name == "Zone A"
        assert gf.lat == 25.0
        assert gf.radius_km == 100

    def test_create_adds_to_list(self):
        mgr = GeofenceManager()
        mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        assert len(mgr.list()) == 1
        assert mgr.list()[0]["name"] == "Zone A"

    def test_name_is_stripped(self):
        mgr = GeofenceManager()
        gf = mgr.create("  Zone A  ", lat=25.0, lon=55.0, radius_km=100)
        assert gf.name == "Zone A"

    @pytest.mark.parametrize("name,lat,lon,radius", [
        ("", 25.0, 55.0, 100),
        ("   ", 25.0, 55.0, 100),
        ("Zone", 95.0, 55.0, 100),
        ("Zone", -95.0, 55.0, 100),
        ("Zone", 25.0, 200.0, 100),
        ("Zone", 25.0, -200.0, 100),
        ("Zone", 25.0, 55.0, 0),
        ("Zone", 25.0, 55.0, -5),
        ("Zone", 25.0, 55.0, 50000),
    ])
    def test_invalid_params_raise_geofence_error(self, name, lat, lon, radius):
        mgr = GeofenceManager()
        with pytest.raises(GeofenceError):
            mgr.create(name, lat, lon, radius)

    def test_geofence_error_is_a_value_error(self):
        assert issubclass(GeofenceError, ValueError)


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

class TestGeofenceDeletion:
    def test_delete_existing_returns_true(self):
        mgr = GeofenceManager()
        gf = mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        assert mgr.delete(gf.id) is True
        assert mgr.list() == []

    def test_delete_unknown_id_returns_false_not_raises(self):
        mgr = GeofenceManager()
        assert mgr.delete("nonexistent") is False

    def test_delete_clears_containment_state(self):
        mgr = GeofenceManager()
        gf = mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        obj = {"id": "o1", "name": "Ship", "type": "ship", "lat": 25.0, "lon": 55.0}
        mgr.update([obj])  # establishes containment state
        assert len(mgr._containment) == 1
        mgr.delete(gf.id)
        assert len(mgr._containment) == 0


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------

class TestContainment:
    def test_contains_point_inside_radius(self):
        mgr = GeofenceManager()
        gf = mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        assert gf.contains(25.0, 55.0) is True  # exact center

    def test_does_not_contain_point_outside_radius(self):
        mgr = GeofenceManager()
        gf = mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        assert gf.contains(50.0, 50.0) is False  # far away

    def test_boundary_is_inclusive(self):
        mgr = GeofenceManager()
        # Derive the exact distance rather than assuming "1 degree ~ 111km"
        # -- that approximation is close enough for humans but not exact
        # enough to test a <= boundary condition against.
        from geofence import haversine_km
        exact_distance = haversine_km(0.0, 0.0, 1.0, 0.0)
        gf = mgr.create("Zone A", lat=0.0, lon=0.0, radius_km=exact_distance)
        assert gf.contains(1.0, 0.0) is True

    def test_just_outside_boundary_is_excluded(self):
        mgr = GeofenceManager()
        from geofence import haversine_km
        exact_distance = haversine_km(0.0, 0.0, 1.0, 0.0)
        gf = mgr.create("Zone A", lat=0.0, lon=0.0, radius_km=exact_distance - 1)
        assert gf.contains(1.0, 0.0) is False


# ---------------------------------------------------------------------------
# Enter/exit transition detection — the actual point of the feature
# ---------------------------------------------------------------------------

class TestTransitionDetection:
    def _ship(self, lat, lon, obj_id="o1"):
        return {"id": obj_id, "name": "Test Ship", "type": "ship", "lat": lat, "lon": lon}

    def test_no_geofences_returns_no_alerts(self):
        mgr = GeofenceManager()
        alerts = mgr.update([self._ship(25.0, 55.0)])
        assert alerts == []

    def test_staying_outside_produces_no_alert(self):
        mgr = GeofenceManager()
        mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        obj = self._ship(50.0, 50.0)
        assert mgr.update([obj]) == []
        assert mgr.update([obj]) == []

    def test_moving_inside_fires_enter(self):
        mgr = GeofenceManager()
        gf = mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        obj = self._ship(50.0, 50.0)
        mgr.update([obj])  # establish "outside" baseline

        obj["lat"], obj["lon"] = 25.0, 55.0
        alerts = mgr.update([obj])
        assert len(alerts) == 1
        assert alerts[0]["event"] == "enter"
        assert alerts[0]["geofence_id"] == gf.id
        assert alerts[0]["object_id"] == "o1"

    def test_staying_inside_after_entering_produces_no_new_alert(self):
        mgr = GeofenceManager()
        mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        obj = self._ship(25.0, 55.0)
        mgr.update([obj])  # first tick, already inside -> fires enter
        alerts = mgr.update([obj])  # second tick, still inside
        assert alerts == []

    def test_moving_outside_after_inside_fires_exit(self):
        mgr = GeofenceManager()
        mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        obj = self._ship(25.0, 55.0)
        mgr.update([obj])  # inside

        obj["lat"], obj["lon"] = 50.0, 50.0
        alerts = mgr.update([obj])
        assert len(alerts) == 1
        assert alerts[0]["event"] == "exit"

    def test_object_already_inside_on_first_ever_check_fires_enter(self):
        """Deliberate behavior: an object already inside a brand-new
        geofence should be alerted immediately, not silently ignored."""
        mgr = GeofenceManager()
        mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        obj = self._ship(25.01, 55.01)
        alerts = mgr.update([obj])
        assert len(alerts) == 1
        assert alerts[0]["event"] == "enter"

    def test_multiple_objects_tracked_independently(self):
        mgr = GeofenceManager()
        mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        inside_obj = self._ship(25.0, 55.0, obj_id="inside")
        outside_obj = self._ship(50.0, 50.0, obj_id="outside")

        alerts = mgr.update([inside_obj, outside_obj])
        assert len(alerts) == 1
        assert alerts[0]["object_id"] == "inside"

    def test_multiple_geofences_tracked_independently(self):
        mgr = GeofenceManager()
        gf_a = mgr.create("Zone A", lat=25.0, lon=55.0, radius_km=100)
        gf_b = mgr.create("Zone B", lat=50.0, lon=50.0, radius_km=100)
        obj = self._ship(25.0, 55.0)  # inside A, outside B

        alerts = mgr.update([obj])
        assert len(alerts) == 1
        assert alerts[0]["geofence_id"] == gf_a.id

    def test_full_enter_then_exit_sequence(self):
        mgr = GeofenceManager()
        mgr.create("Zone A", lat=0.0, lon=0.0, radius_km=50)
        obj = self._ship(10.0, 10.0)  # starts far outside

        assert mgr.update([obj]) == []  # tick 1: outside, baseline

        obj["lat"], obj["lon"] = 0.0, 0.0
        alerts = mgr.update([obj])  # tick 2: enters
        assert [a["event"] for a in alerts] == ["enter"]

        assert mgr.update([obj]) == []  # tick 3: stays inside

        obj["lat"], obj["lon"] = 10.0, 10.0
        alerts = mgr.update([obj])  # tick 4: exits
        assert [a["event"] for a in alerts] == ["exit"]

        assert mgr.update([obj]) == []  # tick 5: stays outside
