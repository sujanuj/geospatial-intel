"""Tests for simulator.py.

Covers: object creation, motion model correctness, status distribution,
reverse-geocoding integration, and the throttled regeocode schedule.
"""

import math
import statistics

import pytest

from simulator import (
    GeoObject,
    ObjectSimulator,
    ObjectStatus,
    ObjectType,
    _country_name,
    move_object,
    random_status,
)


# ---------------------------------------------------------------------------
# Object creation
# ---------------------------------------------------------------------------

class TestObjectCreation:
    def test_creates_requested_counts_per_type(self):
        sim = ObjectSimulator(n_ships=15, n_aircraft=10, n_vehicles=10)
        stats = sim.get_stats()
        assert stats["ships"] == 15
        assert stats["aircraft"] == 10
        assert stats["vehicles"] == 10
        assert stats["total"] == 35

    def test_handles_zero_counts(self):
        sim = ObjectSimulator(n_ships=0, n_aircraft=0, n_vehicles=0)
        assert sim.get_stats()["total"] == 0
        assert sim.get_all() == []

    def test_every_object_has_valid_fields_after_init(self):
        sim = ObjectSimulator(n_ships=5, n_aircraft=5, n_vehicles=5)
        for obj in sim.get_all():
            assert obj["id"]
            assert obj["name"]
            assert obj["type"] in ("ship", "aircraft", "vehicle")
            assert obj["status"] in ("active", "warning", "threat")
            # country must be resolved (non-empty) immediately after init,
            # not left as the "" placeholder used before refresh_countries()
            assert obj["country"] != ""
            assert -85 <= obj["lat"] <= 85
            assert -180 <= obj["lon"] <= 180

    def test_object_ids_are_unique(self):
        sim = ObjectSimulator(n_ships=15, n_aircraft=10, n_vehicles=10)
        ids = [obj["id"] for obj in sim.get_all()]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Motion model
# ---------------------------------------------------------------------------

class TestMoveObject:
    def _make_ship(self, lat=0.0, lon=0.0, heading=0.0, speed=15.0):
        return GeoObject(
            id="test",
            name="Test Ship",
            type=ObjectType.SHIP,
            lat=lat,
            lon=lon,
            heading=heading,
            speed=speed,
            status=ObjectStatus.ACTIVE,
            altitude=0,
            country="USA",
        )

    def test_heading_zero_moves_north_increases_lat(self):
        # heading=0 means "due north" per math.cos/sin convention used here
        obj = self._make_ship(lat=0.0, lon=0.0, heading=0.0)
        lat0 = obj.lat
        move_object(obj, dt=1.0)
        # heading jitters by up to +-2 degrees, so allow tolerance, but the
        # dominant motion at heading~0 should be a latitude increase
        assert obj.lat > lat0

    def test_object_actually_moves_given_nonzero_speed(self):
        """Regression test for the km/degree unit bug: a ship at a normal
        cruising speed must move a measurable amount in one tick, not an
        imperceptible ~1e-6 degree fraction."""
        obj = self._make_ship(speed=15.0)
        lat0, lon0 = obj.lat, obj.lon
        move_object(obj, dt=1.0)
        dist_km = math.hypot(obj.lat - lat0, obj.lon - lon0) * 111
        # With the unit bug, this would be ~0.00003 km. After the fix it
        # should be on the order of a few hundred meters per tick.
        assert dist_km > 0.05, (
            f"ship moved only {dist_km:.6f} km in one tick — "
            "looks like the km-per-degree unit bug regressed"
        )

    def test_aircraft_moves_faster_than_ship_at_realistic_speeds(self):
        """Aircraft (300-700kt) should cover meaningfully more ground per
        tick than ships (2-25kt) — this is the real-world speed ratio the
        SIM_SPEED_MULTIPLIER fix was designed to preserve, unlike the old
        code's inconsistent per-type multipliers."""
        ship = self._make_ship(speed=15.0)
        aircraft = GeoObject(
            id="a", name="Test Aircraft", type=ObjectType.AIRCRAFT,
            lat=0.0, lon=0.0, heading=0.0, speed=500.0,
            status=ObjectStatus.ACTIVE, altitude=30000, country="USA",
        )
        ship_lat0, ship_lon0 = ship.lat, ship.lon
        air_lat0, air_lon0 = aircraft.lat, aircraft.lon
        move_object(ship, dt=1.0)
        move_object(aircraft, dt=1.0)
        ship_dist = math.hypot(ship.lat - ship_lat0, ship.lon - ship_lon0)
        air_dist = math.hypot(aircraft.lat - air_lat0, aircraft.lon - air_lon0)
        assert air_dist > ship_dist * 5

    def test_trail_caps_at_20_points(self):
        obj = self._make_ship()
        for _ in range(30):
            move_object(obj, dt=1.0)
        assert len(obj.trail) == 20

    def test_latitude_clamped_to_valid_range(self):
        obj = self._make_ship(lat=84.9, heading=0.0, speed=25.0)
        for _ in range(50):
            move_object(obj, dt=1.0)
        assert -85 <= obj.lat <= 85

    def test_longitude_wraps_instead_of_exceeding_180(self):
        obj = self._make_ship(lon=179.9, heading=90.0, speed=700.0)
        obj.type = ObjectType.AIRCRAFT  # fast mover, easiest to force a wrap
        for _ in range(20):
            move_object(obj, dt=1.0)
        assert -180 <= obj.lon <= 180

    def test_speed_stays_within_type_specific_bounds(self):
        ship = self._make_ship(speed=15.0)
        for _ in range(200):
            move_object(ship, dt=1.0)
            assert 2 <= ship.speed <= 25


# ---------------------------------------------------------------------------
# Status distribution
# ---------------------------------------------------------------------------

class TestRandomStatus:
    def test_distribution_converges_to_70_20_10(self):
        n = 20000
        counts = {"active": 0, "warning": 0, "threat": 0}
        for _ in range(n):
            counts[random_status().value] += 1
        active_frac = counts["active"] / n
        warning_frac = counts["warning"] / n
        threat_frac = counts["threat"] / n
        # generous tolerance since this is a statistical test, not exact
        assert abs(active_frac - 0.70) < 0.02
        assert abs(warning_frac - 0.20) < 0.02
        assert abs(threat_frac - 0.10) < 0.02

    def test_always_returns_a_valid_status(self):
        seen = {random_status() for _ in range(500)}
        assert seen <= {ObjectStatus.ACTIVE, ObjectStatus.WARNING, ObjectStatus.THREAT}


# ---------------------------------------------------------------------------
# Reverse geocoding
# ---------------------------------------------------------------------------

class TestReverseGeocoding:
    def test_country_name_maps_known_code(self):
        assert _country_name("US") == "USA"
        assert _country_name("GB") == "UK"
        assert _country_name("AE") == "UAE"

    def test_country_name_falls_back_to_raw_code_for_unknown(self):
        # must never silently drop a country — unknown codes fall back to
        # the raw ISO code rather than an empty string or crash
        assert _country_name("ZZ") == "ZZ"

    def test_refresh_countries_resolves_known_coordinates(self):
        sim = ObjectSimulator(n_ships=1, n_aircraft=0, n_vehicles=0)
        obj = list(sim.objects.values())[0]
        # Persian Gulf, near Dubai
        obj.lat, obj.lon = 25.2048, 55.2708
        sim.refresh_countries()
        assert obj.country == "UAE"

    def test_refresh_countries_handles_empty_object_set(self):
        sim = ObjectSimulator(n_ships=0, n_aircraft=0, n_vehicles=0)
        # must not raise on an empty fleet
        sim.refresh_countries()

    def test_country_refresh_is_throttled_not_every_tick(self):
        """Regression test: country should only be re-resolved every
        REGEOCODE_EVERY_N_TICKS ticks, since the geocode call is
        synchronous CPU work that would block the event loop if run
        every second."""
        sim = ObjectSimulator(n_ships=1, n_aircraft=0, n_vehicles=0)
        obj_id = list(sim.objects.keys())[0]

        # Force the object far from its current country, then confirm the
        # label does NOT change on a tick that isn't a multiple of the
        # regeocode interval.
        obj = sim.objects[obj_id]
        obj.lat, obj.lon = 51.5074, -0.1278  # London
        sim.refresh_countries()
        assert obj.country == "UK"

        obj.lat, obj.lon = 35.6762, 139.6503  # Tokyo — different country
        sim._tick_count = 0  # align to a known point in the cycle
        for tick in range(1, sim.REGEOCODE_EVERY_N_TICKS):
            sim.update(dt=0.0)  # dt=0 isolates the geocode timing from motion
            assert sim.objects[obj_id].country == "UK", (
                f"country changed on non-regeocode tick {tick}"
            )
        sim.update(dt=0.0)  # this tick should hit the regeocode interval
        assert sim.objects[obj_id].country == "Japan"


# ---------------------------------------------------------------------------
# ObjectSimulator.update() integration
# ---------------------------------------------------------------------------

class TestSimulatorUpdate:
    def test_update_moves_all_objects(self):
        sim = ObjectSimulator(n_ships=5, n_aircraft=5, n_vehicles=5)
        positions_before = {oid: (o.lat, o.lon) for oid, o in sim.objects.items()}
        sim.update(dt=1.0)
        moved = sum(
            1 for oid, o in sim.objects.items()
            if (o.lat, o.lon) != positions_before[oid]
        )
        # virtually all objects should have moved (heading/speed jitter
        # alone guarantees motion for a nonzero-speed object)
        assert moved == len(sim.objects)

    def test_get_by_id_returns_none_for_unknown_id(self):
        sim = ObjectSimulator(n_ships=1, n_aircraft=0, n_vehicles=0)
        assert sim.get_by_id("does-not-exist") is None

    def test_get_by_id_returns_matching_object(self):
        sim = ObjectSimulator(n_ships=1, n_aircraft=0, n_vehicles=0)
        obj_id = list(sim.objects.keys())[0]
        result = sim.get_by_id(obj_id)
        assert result is not None
        assert result["id"] == obj_id

    def test_stats_counts_match_actual_status_values(self):
        sim = ObjectSimulator(n_ships=10, n_aircraft=10, n_vehicles=10)
        stats = sim.get_stats()
        objs = sim.get_all()
        assert stats["active"] == sum(1 for o in objs if o["status"] == "active")
        assert stats["warning"] == sum(1 for o in objs if o["status"] == "warning")
        assert stats["threat"] == sum(1 for o in objs if o["status"] == "threat")
        assert stats["active"] + stats["warning"] + stats["threat"] == stats["total"]
