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


class ObjectType(str, Enum):
    SHIP = "ship"
    AIRCRAFT = "aircraft"
    VEHICLE = "vehicle"


class ObjectStatus(str, Enum):
    ACTIVE = "active"
    WARNING = "warning"
    THREAT = "threat"


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


# Starting positions for different object types.
# Each tuple is (lat, lon, region_label, country) — country matches the
# actual real-world location so the UI's country field is consistent with
# where the object actually is, instead of being assigned independently.
SHIP_STARTS = [
    (37.7749, -122.4194, "Pacific Ocean", "USA"),        # San Francisco coast
    (40.6892, -74.0445, "Atlantic Ocean", "USA"),        # New York harbor
    (51.5074, -0.1278, "English Channel", "UK"),         # London area
    (35.6762, 139.6503, "Pacific", "Japan"),              # Tokyo bay
    (1.3521, 103.8198, "Singapore Strait", "Singapore"), # Singapore
    (25.2048, 55.2708, "Persian Gulf", "UAE"),            # Dubai
    (-33.8688, 151.2093, "Tasman Sea", "Australia"),      # Sydney
    (48.8566, 2.3522, "Atlantic", "France"),              # Paris/Le Havre
]

AIRCRAFT_STARTS = [
    (40.7128, -74.0060, "North America", "USA"),
    (51.5074, -0.1278, "Europe", "UK"),
    (35.6762, 139.6503, "Asia Pacific", "Japan"),
    (25.2048, 55.2708, "Middle East", "UAE"),
    (-23.5505, -46.6333, "South America", "Brazil"),
    (19.0760, 72.8777, "South Asia", "India"),
    (55.7558, 37.6173, "Eastern Europe", "Russia"),
    (30.0444, 31.2357, "North Africa", "Egypt"),
]

VEHICLE_STARTS = [
    (37.3861, -122.0839, "Silicon Valley", "USA"),
    (40.7128, -74.0060, "New York", "USA"),
    (51.5074, -0.1278, "London", "UK"),
    (48.8566, 2.3522, "Paris", "France"),
    (52.5200, 13.4050, "Berlin", "Germany"),
    (35.6762, 139.6503, "Tokyo", "Japan"),
    (28.6139, 77.2090, "Delhi", "India"),
    (39.9042, 116.4074, "Beijing", "China"),
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
    """
    # Slightly vary heading and speed for realistic movement
    obj.heading = (obj.heading + random.uniform(-2, 2)) % 360
    if obj.type == ObjectType.SHIP:
        obj.speed = max(2, min(25, obj.speed + random.uniform(-0.5, 0.5)))
        speed_deg_per_sec = obj.speed * 0.000514 / 111000  # knots to deg/s
    elif obj.type == ObjectType.AIRCRAFT:
        obj.speed = max(300, min(700, obj.speed + random.uniform(-5, 5)))
        speed_deg_per_sec = obj.speed * 0.000277 / 111000  # knots to deg/s
        # Scale up for visible movement in demo
        speed_deg_per_sec *= 50
    else:  # vehicle
        obj.speed = max(10, min(100, obj.speed + random.uniform(-2, 2)))
        speed_deg_per_sec = obj.speed * 0.000447 / 111000  # mph to deg/s
        speed_deg_per_sec *= 20

    # Scale ships for visible movement
    if obj.type == ObjectType.SHIP:
        speed_deg_per_sec *= 100

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

    def __init__(self, n_ships: int = 15, n_aircraft: int = 10, n_vehicles: int = 10):
        self.objects: Dict[str, GeoObject] = {}
        self._create_objects(n_ships, n_aircraft, n_vehicles)

    def _create_objects(self, n_ships: int, n_aircraft: int, n_vehicles: int):
        # Ships
        for i in range(n_ships):
            lat, lon, _, country = random.choice(SHIP_STARTS)
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
                country=country,
            )

        # Aircraft
        for i in range(n_aircraft):
            lat, lon, _, country = random.choice(AIRCRAFT_STARTS)
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
                country=country,
            )

        # Vehicles
        for i in range(n_vehicles):
            lat, lon, _, country = random.choice(VEHICLE_STARTS)
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
                country=country,
            )

    def update(self, dt: float = 1.0):
        """Move all objects and randomly change some statuses."""
        for obj in self.objects.values():
            move_object(obj, dt)
            # Randomly change status occasionally
            if random.random() < 0.01:
                obj.status = random_status()

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
