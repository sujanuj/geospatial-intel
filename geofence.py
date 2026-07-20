"""Circular geofences with live enter/exit alerting.

A geofence here is a circle: a center (lat, lon) and a radius in km. This
is a deliberate simplification over arbitrary polygons -- it covers the
common "alert me if anything gets within N km of this point" case, which
is most of what real geofencing is used for, without needing a
point-in-polygon algorithm or a map-drawing library on the frontend.

GeofenceManager tracks, per (geofence, object) pair, whether the object
was inside the geofence on the *previous* tick. Comparing that against the
current tick is what turns "is X currently inside Y" (a snapshot fact)
into "X just entered/exited Y" (an event) -- the actual point of a
geofence alert system. Without that state, you could only ever report
containment, never a crossing.
"""

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class GeofenceError(ValueError):
    """Raised for invalid geofence parameters (bad radius, out-of-range
    lat/lon, etc). Subclasses ValueError so callers can catch broadly."""


EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in km.

    Standard haversine formula. Implemented directly rather than pulling in
    a geo library -- this is the one piece of real geometry the whole
    feature depends on, so it's worth having it visible and tested rather
    than trusting an opaque dependency.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c


@dataclass
class Geofence:
    id: str
    name: str
    lat: float
    lon: float
    radius_km: float
    created_at: float = field(default_factory=time.time)

    def contains(self, lat: float, lon: float) -> bool:
        return haversine_km(self.lat, self.lon, lat, lon) <= self.radius_km

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "radius_km": self.radius_km,
            "created_at": self.created_at,
        }


def _validate_geofence_params(name: str, lat: float, lon: float, radius_km: float) -> None:
    if not name or not name.strip():
        raise GeofenceError("geofence name cannot be empty")
    if not (-90 <= lat <= 90):
        raise GeofenceError(f"lat must be between -90 and 90, got {lat}")
    if not (-180 <= lon <= 180):
        raise GeofenceError(f"lon must be between -180 and 180, got {lon}")
    if radius_km <= 0:
        raise GeofenceError(f"radius_km must be positive, got {radius_km}")
    if radius_km > 20000:
        # larger than any possible great-circle distance on Earth -- almost
        # certainly a units mistake (e.g. meters passed where km expected)
        raise GeofenceError(
            f"radius_km of {radius_km} is larger than the Earth's circumference; "
            "did you mean to pass meters instead of km?"
        )


class GeofenceManager:
    """Owns the set of active geofences and detects enter/exit events."""

    def __init__(self):
        self.geofences: Dict[str, Geofence] = {}
        # (geofence_id, object_id) -> True if that object was inside that
        # geofence as of the last update() call.
        self._containment: Dict[Tuple[str, str], bool] = {}

    def create(self, name: str, lat: float, lon: float, radius_km: float) -> Geofence:
        _validate_geofence_params(name, lat, lon, radius_km)
        gf = Geofence(id=str(uuid.uuid4())[:8], name=name.strip(), lat=lat, lon=lon, radius_km=radius_km)
        self.geofences[gf.id] = gf
        return gf

    def delete(self, geofence_id: str) -> bool:
        """Returns True if a geofence was actually removed, False if the
        id didn't exist (not an error -- deleting something already gone
        is a no-op, not a failure)."""
        if geofence_id not in self.geofences:
            return False
        del self.geofences[geofence_id]
        # drop any tracked containment state for this geofence so it
        # doesn't leak memory or produce stale alerts if the id is reused
        stale_keys = [k for k in self._containment if k[0] == geofence_id]
        for k in stale_keys:
            del self._containment[k]
        return True

    def list(self) -> List[dict]:
        return [gf.to_dict() for gf in self.geofences.values()]

    def update(self, objects: List[dict]) -> List[dict]:
        """Check every (geofence, object) pair against the object's
        current position and return alert events for anything that
        entered or exited since the last call.

        Each alert dict: {geofence_id, geofence_name, object_id,
        object_name, object_type, event: "enter"|"exit", timestamp}.

        Deliberate behavior worth calling out: if an object is already
        inside a geofence the very first time this is called for that
        (geofence, object) pair -- e.g. a geofence was just drawn around
        something that's already there -- this fires an "enter" alert
        immediately, since there's no prior "was_inside" state to compare
        against (it defaults to False). This is intentional, not a bug:
        an object already sitting inside a newly-created zone is exactly
        the kind of thing a real alert system should surface right away,
        not suppress because there was no observed "crossing."
        """
        if not self.geofences:
            return []

        alerts: List[dict] = []
        now = time.time()
        seen_object_ids = {obj["id"] for obj in objects}

        for gf in self.geofences.values():
            for obj in objects:
                key = (gf.id, obj["id"])
                currently_inside = gf.contains(obj["lat"], obj["lon"])
                was_inside = self._containment.get(key, False)

                if currently_inside and not was_inside:
                    alerts.append({
                        "geofence_id": gf.id,
                        "geofence_name": gf.name,
                        "object_id": obj["id"],
                        "object_name": obj["name"],
                        "object_type": obj["type"],
                        "event": "enter",
                        "timestamp": now,
                    })
                elif was_inside and not currently_inside:
                    alerts.append({
                        "geofence_id": gf.id,
                        "geofence_name": gf.name,
                        "object_id": obj["id"],
                        "object_name": obj["name"],
                        "object_type": obj["type"],
                        "event": "exit",
                        "timestamp": now,
                    })

                self._containment[key] = currently_inside

        # Prune containment entries for objects that no longer exist (e.g.
        # if the simulator were ever reconfigured with a different fleet
        # mid-run) -- prevents unbounded growth over a long-running process.
        stale_keys = [k for k in self._containment if k[1] not in seen_object_ids]
        for k in stale_keys:
            del self._containment[k]

        return alerts
