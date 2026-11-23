"""Timetable index: loading, service calendar, stop lookup.

The index is built offline from a GTFS(-JP) zip by ``tools/build_index.py``
and stored in S3 as gzip-compressed JSON. Lambda loads it once per container.
"""

from __future__ import annotations

import gzip
import json
import math
import unicodedata
from dataclasses import dataclass
from datetime import date

WALK_SPEED_MPS = 1.2  # walking speed (m/s)
WALK_DETOUR = 1.3  # straight-line distance -> street distance factor


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walk_seconds(distance_m: float) -> int:
    return int(distance_m * WALK_DETOUR / WALK_SPEED_MPS)


_STRIP_SUFFIXES = ("バス停留所", "バス停", "停留所", "のりば", "乗り場")


def normalize_name(name: str) -> str:
    """Normalize a stop/place name for matching (width, spaces, common suffixes)."""
    s = unicodedata.normalize("NFKC", name).strip().lower()
    s = "".join(s.split())
    for suffix in _STRIP_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s


@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Trip:
    trip_id: str
    route_name: str
    headsign: str
    service_id: str


class Timetable:
    """In-memory timetable built from the index dict."""

    def __init__(self, index: dict):
        self.meta: dict = index.get("meta", {})
        self.stops: list[Stop] = [Stop(*s) for s in index["stops"]]
        self.trips: list[Trip] = [Trip(*t) for t in index["trips"]]
        self.services: dict[str, dict] = index["services"]
        # connection: (dep_stop, arr_stop, dep_sec, arr_sec, trip_idx, flags), sorted by dep_sec
        # flags bit0 = boarding allowed at dep_stop, bit1 = alighting allowed at arr_stop
        self.connections: list[tuple[int, int, int, int, int, int]] = [
            (*c, 3) if len(c) == 5 else tuple(c) for c in index["connections"]
        ]
        self.dep_times: list[int] = [c[2] for c in self.connections]
        self.footpaths: dict[int, list[tuple[int, int]]] = {
            int(k): [(int(o), int(w)) for o, w in v] for k, v in index.get("footpaths", {}).items()
        }
        self._by_name: dict[str, list[int]] = {}
        for i, stop in enumerate(self.stops):
            self._by_name.setdefault(normalize_name(stop.name), []).append(i)

    # ---- loading -------------------------------------------------------
    @classmethod
    def from_bytes(cls, data: bytes) -> "Timetable":
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return cls(json.loads(data))

    # ---- calendar ------------------------------------------------------
    def active_trips(self, day: date) -> set[int]:
        ymd = day.strftime("%Y%m%d")
        weekday = day.weekday()  # Mon=0
        active_services = set()
        for service_id, svc in self.services.items():
            if ymd in svc.get("remove", []):
                continue
            if ymd in svc.get("add", []):
                active_services.add(service_id)
                continue
            start, end = svc.get("start"), svc.get("end")
            days = svc.get("days")
            if days and start and end and start <= ymd <= end and days[weekday]:
                active_services.add(service_id)
        return {i for i, t in enumerate(self.trips) if t.service_id in active_services}

    # ---- stop lookup ---------------------------------------------------
    def find_stops_by_name(self, query: str) -> list[int]:
        """Return stop indexes whose name matches the query (exact, then partial)."""
        q = normalize_name(query)
        if not q:
            return []
        if q in self._by_name:
            return list(self._by_name[q])
        # Partial match: prefer the shortest stop name that contains the query.
        partial = [(len(n), n) for n in self._by_name if q in n]
        if partial:
            best = min(partial)[1]
            return list(self._by_name[best])
        # Query contains a stop name (e.g. "市役所前から" style leftovers).
        contained = [(len(n), n) for n in self._by_name if len(n) >= 2 and n in q]
        if contained:
            best = max(contained)[1]
            return list(self._by_name[best])
        return []

    def stops_near(self, lat: float, lon: float, radius_m: float = 800, limit: int = 8) -> list[tuple[int, int]]:
        """Return [(stop_idx, walk_seconds)] for stops within radius, nearest first."""
        found = []
        for i, stop in enumerate(self.stops):
            d = haversine_m(lat, lon, stop.lat, stop.lon)
            if d <= radius_m:
                found.append((d, i))
        found.sort()
        return [(i, walk_seconds(d)) for d, i in found[:limit]]
