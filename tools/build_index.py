"""Build the timetable index (gzip JSON) from a GTFS / GTFS-JP zip.

Usage:
    python3 tools/build_index.py <gtfs.zip> <out.json.gz> [--name "地域名"]

Then upload it to the data bucket (see README).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lambda" / "bus_route"))
from timetable import haversine_m, walk_seconds  # noqa: E402

FOOTPATH_RADIUS_M = 300


def read_csv(zf: zipfile.ZipFile, name: str) -> list[dict]:
    member = next((m for m in zf.namelist() if m.split("/")[-1] == name), None)
    if member is None:
        return []
    raw = zf.read(member)
    text = raw.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def parse_time(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    h, m, s = (int(x) for x in value.split(":"))
    return h * 3600 + m * 60 + s


def build(zip_path: str, name: str) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        stops_rows = read_csv(zf, "stops.txt")
        routes_rows = read_csv(zf, "routes.txt")
        trips_rows = read_csv(zf, "trips.txt")
        stop_times_rows = read_csv(zf, "stop_times.txt")
        calendar_rows = read_csv(zf, "calendar.txt")
        calendar_dates_rows = read_csv(zf, "calendar_dates.txt")
        agency_rows = read_csv(zf, "agency.txt")

    # Stops: only boardable stops/poles (location_type 0 or empty)
    stops = []
    stop_index: dict[str, int] = {}
    for r in stops_rows:
        if (r.get("location_type") or "0").strip() not in ("", "0"):
            continue
        try:
            lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
        except (KeyError, ValueError):
            continue
        stop_index[r["stop_id"]] = len(stops)
        stops.append([r["stop_id"], r.get("stop_name", "").strip(), lat, lon])

    route_names = {}
    for r in routes_rows:
        route_names[r["route_id"]] = (r.get("route_short_name") or r.get("route_long_name") or r["route_id"]).strip()

    trips = []
    trip_index: dict[str, int] = {}
    for r in trips_rows:
        trip_index[r["trip_id"]] = len(trips)
        trips.append([
            r["trip_id"],
            route_names.get(r.get("route_id", ""), ""),
            (r.get("trip_headsign") or "").strip(),
            r.get("service_id", ""),
        ])

    services: dict[str, dict] = {}
    for r in calendar_rows:
        services[r["service_id"]] = {
            "days": [int(r.get(d, "0") or 0) for d in
                     ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")],
            "start": r.get("start_date", ""),
            "end": r.get("end_date", ""),
            "add": [],
            "remove": [],
        }
    for r in calendar_dates_rows:
        svc = services.setdefault(r["service_id"], {"days": None, "start": None, "end": None, "add": [], "remove": []})
        (svc["add"] if r.get("exception_type") == "1" else svc["remove"]).append(r["date"])

    # Connections from consecutive stop_times of each trip
    # (stop_sequence, arrival, departure, stop_idx, can_board, can_alight)
    by_trip: dict[str, list[tuple]] = defaultdict(list)
    for r in stop_times_rows:
        if r["stop_id"] not in stop_index or r["trip_id"] not in trip_index:
            continue
        arr = parse_time(r.get("arrival_time", ""))
        dep = parse_time(r.get("departure_time", ""))
        can_board = (r.get("pickup_type") or "0").strip() != "1"
        can_alight = (r.get("drop_off_type") or "0").strip() != "1"
        by_trip[r["trip_id"]].append(
            (int(r["stop_sequence"]), arr, dep, stop_index[r["stop_id"]], can_board, can_alight))

    # connection: [dep_stop, arr_stop, dep_sec, arr_sec, trip_idx, flags]
    # flags bit0 = boarding allowed at dep_stop, bit1 = alighting allowed at arr_stop
    connections = []
    for trip_id, rows in by_trip.items():
        rows.sort()
        t_idx = trip_index[trip_id]
        for a, b in zip(rows, rows[1:]):
            _, _, dep, s1, board, _ = a
            _, arr, dep2, s2, _, alight = b
            arr = arr if arr is not None else dep2
            if dep is None or arr is None or s1 == s2:
                continue
            flags = (1 if board else 0) | (2 if alight else 0)
            connections.append([s1, s2, dep, arr, t_idx, flags])
    connections.sort(key=lambda c: (c[2], c[3]))

    # Footpaths between nearby stops (grid bucketing keeps it fast)
    cell = FOOTPATH_RADIUS_M / 111000.0
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (_, _, lat, lon) in enumerate(stops):
        grid[(math.floor(lat / cell), math.floor(lon / cell))].append(i)
    footpaths: dict[int, list[list[int]]] = defaultdict(list)
    for (gy, gx), members in grid.items():
        neighbours = [j for dy in (-1, 0, 1) for dx in (-1, 0, 1) for j in grid.get((gy + dy, gx + dx), [])]
        for i in members:
            _, _, lat1, lon1 = stops[i]
            for j in neighbours:
                if i == j:
                    continue
                d = haversine_m(lat1, lon1, stops[j][2], stops[j][3])
                if d <= FOOTPATH_RADIUS_M:
                    footpaths[i].append([j, max(60, walk_seconds(d))])

    return {
        "meta": {
            "name": name,
            "agencies": [a.get("agency_name", "") for a in agency_rows],
            "built_at": datetime.now(timezone.utc).isoformat(),
            "source": Path(zip_path).name,
            "counts": {"stops": len(stops), "trips": len(trips), "connections": len(connections)},
        },
        "stops": stops,
        "trips": trips,
        "services": services,
        "connections": connections,
        "footpaths": {str(k): v for k, v in footpaths.items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gtfs_zip")
    ap.add_argument("out")
    ap.add_argument("--name", default="", help="地域名（回答文に使う）")
    args = ap.parse_args()
    index = build(args.gtfs_zip, args.name)
    data = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode()
    with open(args.out, "wb") as f:
        f.write(gzip.compress(data))
    print(json.dumps(index["meta"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
