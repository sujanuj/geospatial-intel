"""Simulates moving objects (ships, aircraft, vehicles) on a world map.

Each object has:
  - Unique ID and name
  - Type: ship | aircraft | vehicle
  - Position (lat, lon)
  - Speed and heading
  - Status: active | warning | threat
  - History trail (last 20 positions)

Objects move realistically:
  - Ships move slowly (5-20 knots) and follow ocean paths
  - Aircraft move fast (400-600 knots) in straight lines
  - Vehicles move on land at medium speed (30-80 mph)

The simulator runs in a background thread and updates every second.
"""

import math
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
from enum import Enum

import reverse_geocoder as rg


class ObjectType(str, Enum):
    SHIP = "ship"
    AIRCRAFT = "aircraft"
    VEHICLE = "vehicle"


class ObjectStatus(str, Enum):
    ACTIVE = "active"
    WARNING = "warning"
    THREAT = "threat"


# reverse_geocoder returns ISO 3166-1 alpha-2 codes (e.g. "US", "GB"), not
# display names. This maps the codes we're likely to see to a friendlier
# label; anything not in the map falls back to the raw code, so nothing is
# ever silently dropped.
COUNTRY_CODE_NAMES = {
    "US": "USA", "GB": "UK", "JP": "Japan", "SG": "Singapore",
    "AE": "UAE", "AU": "Australia", "FR": "France", "DE": "Germany",
    "IN": "India", "CN": "China", "BR": "Brazil", "RU": "Russia",
    "EG": "Egypt", "CA": "Canada", "ID": "Indonesia", "MX": "Mexico",
    "ZA": "South Africa", "SA": "Saudi Arabia", "IT": "Italy",
    "ES": "Spain", "KR": "South Korea", "TH": "Thailand",
    "MY": "Malaysia", "PH": "Philippines", "VN": "Vietnam",
    "AR": "Argentina", "CL": "Chile", "NZ": "New Zealand",
    "NO": "Norway", "SE": "Sweden", "FI": "Finland", "PT": "Portugal",
    "NL": "Netherlands", "TR": "Turkey", "GR": "Greece", "PK": "Pakistan",
    "IR": "Iran", "IQ": "Iraq", "KE": "Kenya", "NG": "Nigeria",
    "PE": "Peru", "CO": "Colombia", "PL": "Poland", "UA": "Ukraine",
}


def _country_name(cc: str) -> str:
    return COUNTRY_CODE_NAMES.get(cc, cc)


@dataclass
class GeoObject:
    id: str
    name: str
    type: ObjectType
    lat: float
    lon: float
    heading: float        # degrees 0-360
    speed: float          # knots for ship/aircraft, mph for vehicle
    status: ObjectStatus
    altitude: float       # feet (0 for surface objects)
    country: str
    trail: List[Dict] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        d["status"] = self.status.value
        return d


# Starting positions for different object types (lat, lon, region_label).
# Country is no longer stored here — it's derived live from actual position
# via reverse geocoding, see refresh_countries() below.
SHIP_STARTS = [
    (37.7749, -122.4194, "Pacific Ocean"),   # San Francisco coast
    (40.6892, -74.0445, "Atlantic Ocean"),   # New York harbor
    (51.5074, -0.1278, "English Channel"),   # London area
    (35.6762, 139.6503, "Pacific"),          # Tokyo bay
    (1.3521, 103.8198, "Singapore Strait"),  # Singapore
    (25.2048, 55.2708, "Persian Gulf"),      # Dubai
    (-33.8688, 151.2093, "Tasman Sea"),      # Sydney
    (48.8566, 2.3522, "Atlantic"),           # Paris/Le Havre
]

AIRCRAFT_STARTS = [
    (40.7128, -74.0060, "North America"),
    (51.5074, -0.1278, "Europe"),
    (35.6762, 139.6503, "Asia Pacific"),
    (25.2048, 55.2708, "Middle East"),
    (-23.5505, -46.6333, "South America"),
    (19.0760, 72.8777, "South Asia"),
    (55.7558, 37.6173, "Eastern Europe"),
    (30.0444, 31.2357, "North Africa"),
]

VEHICLE_STARTS = [
    (37.3861, -122.0839, "Silicon Valley"),
    (40.7128, -74.0060, "New York"),
    (51.5074, -0.1278, "London"),
    (48.8566, 2.3522, "Paris"),
    (52.5200, 13.4050, "Berlin"),
    (35.6762, 139.6503, "Tokyo"),
    (28.6139, 77.2090, "Delhi"),
    (39.9042, 116.4074, "Beijing"),
]

SHIP_NAMES = [
    "MV Pacific Star", "SS Atlantic Voyager", "MV Ocean Pioneer",
    "SS Northern Light", "MV Coral Queen", "SS Iron Eagle",
    "MV Blue Horizon", "SS Storm Rider", "MV Pacific Trader",
    "SS Neptune's Pride", "MV Arctic Explorer", "SS Sea Dragon",
]

AIRCRAFT_NAMES = [
    "UAV-Alpha-01", "Recon-Bravo-7", "UAV-Charlie-3",
    "Drone-Delta-9", "UAV-Echo-14", "Recon-Foxtrot-2",
    "UAV-Golf-5", "Patrol-Hotel-8", "UAV-India-11",
    "Recon-Juliet-4", "UAV-Kilo-6", "Patrol-Lima-12",
]

VEHICLE_NAMES = [
    "Unit-Alpha-01", "Convoy-Bravo-3", "Unit-Charlie-7",
    "Patrol-Delta-2", "Unit-Echo-5", "Convoy-Foxtrot-9",
    "Unit-Golf-4", "Patrol-Hotel-6", "Unit-India-8",
    "Convoy-Juliet-1", "Unit-Kilo-11", "Patrol-Lima-3",
]

# How many simulated seconds of real-world motion happen per broadcast tick
# (1 real second, per main.py's broadcast loop). At 1.0 this would be true
# real-time, which is too slow to see on a live map — a 15-knot ship moves
# about 7.7m/sec in reality. This is a deliberate visualization speed-up,
# not a physics change: relative speed differences between ships, aircraft,
# and vehicles are preserved exactly as they are in reality (aircraft really
# do move ~30-50x faster than ships), unlike the old code's inconsistent
# per-type multipliers, which distorted those ratios.
SIM_SPEED_MULTIPLIER = 30

KM_PER_DEGREE = 111.0  # approx, at the equator; longitude is corrected by cos(lat) below


def random_status() -> ObjectStatus:
    r = random.random()
    if r < 0.7:
        return ObjectStatus.ACTIVE
    elif r < 0.9:
        return ObjectStatus.WARNING
    else:
        return ObjectStatus.THREAT


def move_object(obj: GeoObject, dt: float = 1.0) -> GeoObject:
    """Update object position based on speed and heading.

    Uses simple flat-earth approximation for small distances.
    1 degree latitude ≈ 111km
    1 degree longitude ≈ 111km * cos(lat)

    Speed-to-motion conversion: real speed -> real km/s -> real deg/s,
    then scaled by SIM_SPEED_MULTIPLIER for visible demo movement. This
    replaces an earlier version that divided by 111000 (meters per degree)
    instead of 111 (km per degree) -- a 1000x unit error that made
    real-world-correct speeds imperceptible on screen -- and that papered
    over it with three different undocumented per-type multipliers instead
    of fixing the underlying conversion.
    """
    # Slightly vary heading and speed for realistic movement
    obj.heading = (obj.heading + random.uniform(-2, 2)) % 360
    if obj.type == ObjectType.SHIP:
        obj.speed = max(2, min(25, obj.speed + random.uniform(-0.5, 0.5)))
        speed_km_per_sec = obj.speed * 0.000514  # knots -> km/s
    elif obj.type == ObjectType.AIRCRAFT:
        obj.speed = max(300, min(700, obj.speed + random.uniform(-5, 5)))
        speed_km_per_sec = obj.speed * 0.000514  # knots -> km/s
    else:  # vehicle
        obj.speed = max(10, min(100, obj.speed + random.uniform(-2, 2)))
        speed_km_per_sec = obj.speed * 0.000447  # mph -> km/s

    speed_deg_per_sec = (speed_km_per_sec / KM_PER_DEGREE) * SIM_SPEED_MULTIPLIER

    heading_rad = math.radians(obj.heading)
    dlat = speed_deg_per_sec * math.cos(heading_rad) * dt
    dlon = speed_deg_per_sec * math.sin(heading_rad) / max(0.1, math.cos(math.radians(obj.lat))) * dt

    obj.lat = max(-85, min(85, obj.lat + dlat))
    obj.lon = ((obj.lon + dlon + 180) % 360) - 180

    # Save trail (keep last 20 positions)
    obj.trail.append({"lat": round(obj.lat, 6), "lon": round(obj.lon, 6)})
    if len(obj.trail) > 20:
        obj.trail.pop(0)

    obj.last_updated = time.time()
    return obj


class ObjectSimulator:
    """Manages a fleet of simulated geo objects."""

    # How many update() ticks between reverse-geocoding passes. At 1 tick/sec
    # (see main.py's broadcast loop), 5 means countries refresh every 5s —
    # frequent enough to track objects crossing borders, infrequent enough
    # to keep the synchronous geocode call from dominating tick time.
    REGEOCODE_EVERY_N_TICKS = 5

    def __init__(self, n_ships: int = 15, n_aircraft: int = 10, n_vehicles: int = 10):
        self.objects: Dict[str, GeoObject] = {}
        self._tick_count = 0
        self._create_objects(n_ships, n_aircraft, n_vehicles)

    def _create_objects(self, n_ships: int, n_aircraft: int, n_vehicles: int):
        # Ships
        for i in range(n_ships):
            lat, lon, _ = random.choice(SHIP_STARTS)
            obj_id = str(uuid.uuid4())[:8]
            self.objects[obj_id] = GeoObject(
                id=obj_id,
                name=random.choice(SHIP_NAMES) + f" {i+1}",
                type=ObjectType.SHIP,
                lat=lat + random.uniform(-8, 8),
                lon=lon + random.uniform(-8, 8),
                heading=random.uniform(0, 360),
                speed=random.uniform(5, 20),
                status=random_status(),
                altitude=0,
                country="",  # filled in by refresh_countries() below
            )

        # Aircraft
        for i in range(n_aircraft):
            lat, lon, _ = random.choice(AIRCRAFT_STARTS)
            obj_id = str(uuid.uuid4())[:8]
            self.objects[obj_id] = GeoObject(
                id=obj_id,
                name=random.choice(AIRCRAFT_NAMES) + f"-{i+1:02d}",
                type=ObjectType.AIRCRAFT,
                lat=lat + random.uniform(-15, 15),
                lon=lon + random.uniform(-15, 15),
                heading=random.uniform(0, 360),
                speed=random.uniform(400, 600),
                status=random_status(),
                altitude=random.uniform(10000, 45000),
                country="",  # filled in by refresh_countries() below
            )

        # Vehicles
        for i in range(n_vehicles):
            lat, lon, _ = random.choice(VEHICLE_STARTS)
            obj_id = str(uuid.uuid4())[:8]
            self.objects[obj_id] = GeoObject(
                id=obj_id,
                name=random.choice(VEHICLE_NAMES) + f"-{i+1:02d}",
                type=ObjectType.VEHICLE,
                lat=lat + random.uniform(-3, 3),
                lon=lon + random.uniform(-3, 3),
                heading=random.uniform(0, 360),
                speed=random.uniform(30, 80),
                status=random_status(),
                altitude=0,
                country="",  # filled in by refresh_countries() below
            )

        # Resolve every object's real country from its actual position.
        self.refresh_countries()

    def refresh_countries(self):
        """Reverse-geocode every object's live position to its nearest country.

        Uses the offline `reverse_geocoder` package — no network call or API
        key required, since it ships a local index of ~40k populated places.

        Known limitation: reverse_geocoder always returns the nearest
        populated place, even over open ocean. A ship in the middle of the
        Indian Ocean will be labeled with whatever coastal country/city is
        geographically closest, not a maritime "no country" state. This is
        an approximation, not authoritative maritime boundary data — but
        it's a real improvement over a country picked independently of
        position, since the label is now always geographically grounded.
        """
        objs = list(self.objects.values())
        if not objs:
            return
        coords = [(o.lat, o.lon) for o in objs]
        # mode=1 forces single-threaded lookup, which avoids multiprocessing
        # start-up overhead — worth it for small batches like this (35
        # objects), where that overhead would dominate the actual query cost.
        results = rg.search(coords, mode=1)
        for obj, res in zip(objs, results):
            obj.country = _country_name(res["cc"])

    def update(self, dt: float = 1.0):
        """Move all objects and randomly change some statuses."""
        for obj in self.objects.values():
            move_object(obj, dt)
            # Randomly change status occasionally
            if random.random() < 0.01:
                obj.status = random_status()

        # Re-geocoding is synchronous CPU work (~50ms for 35 objects) that
        # would block the asyncio event loop if run every tick. Country
        # doesn't need per-second freshness the way position does, so we
        # only refresh every REGEOCODE_EVERY_N_TICKS ticks instead.
        self._tick_count += 1
        if self._tick_count % self.REGEOCODE_EVERY_N_TICKS == 0:
            self.refresh_countries()

    def get_all(self) -> List[dict]:
        return [obj.to_dict() for obj in self.objects.values()]

    def get_by_id(self, obj_id: str) -> Optional[dict]:
        obj = self.objects.get(obj_id)
        return obj.to_dict() if obj else None

    def get_stats(self) -> dict:
        objs = list(self.objects.values())
        return {
            "total": len(objs),
            "ships": sum(1 for o in objs if o.type == ObjectType.SHIP),
            "aircraft": sum(1 for o in objs if o.type == ObjectType.AIRCRAFT),
            "vehicles": sum(1 for o in objs if o.type == ObjectType.VEHICLE),
            "active": sum(1 for o in objs if o.status == ObjectStatus.ACTIVE),
            "warning": sum(1 for o in objs if o.status == ObjectStatus.WARNING),
            "threat": sum(1 for o in objs if o.status == ObjectStatus.THREAT),
        }
