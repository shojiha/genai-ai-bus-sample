"""Connection Scan Algorithm (earliest arrival) with journey reconstruction.

Reference: Dibbelt, Pajor, Strasser, Wagner, "Connection Scan Algorithm" (2013/2018).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from timetable import Timetable

INF = 10**9
MIN_TRANSFER_SEC = 60  # minimum time to change buses at the same stop
# Extra cost of walking between stops, used only when comparing options, so that
# riders are not told to get off early and walk unless it clearly saves time.
WALK_PENALTY_SEC = 300


@dataclass
class Leg:
    kind: str  # "bus" | "walk"
    from_stop: int | None
    to_stop: int | None
    dep_sec: int
    arr_sec: int
    trip_idx: int | None = None


@dataclass
class Journey:
    legs: list[Leg] = field(default_factory=list)

    @property
    def dep_sec(self) -> int:
        return self.legs[0].dep_sec

    @property
    def arr_sec(self) -> int:
        return self.legs[-1].arr_sec

    @property
    def transfers(self) -> int:
        return max(0, sum(1 for leg in self.legs if leg.kind == "bus") - 1)


def earliest_arrival(
    tt: Timetable,
    sources: list[tuple[int, int]],
    targets: list[tuple[int, int]],
    t0: int,
    active: set[int],
) -> Journey | None:
    """Find the earliest-arrival journey.

    sources/targets: [(stop_idx, walk_seconds)] — walking time from the origin
    to the stop, or from the stop to the destination.
    t0: departure time from the origin, seconds since midnight of the service day.
    active: trip indexes running on the service day.
    """
    n = len(tt.stops)
    earliest = [INF] * n
    # How each stop was reached: None (from origin), ("bus", board_conn, alight_conn), ("walk", from_stop, sec)
    reached_by: list[tuple | None] = [None] * n
    origin_walk: dict[int, int] = {}
    for s, w in sources:
        if t0 + w < earliest[s]:
            earliest[s] = t0 + w
            origin_walk[s] = w
    target_walk = dict(targets)

    trip_board: dict[int, int] = {}  # trip_idx -> boarding connection index
    best_arr = INF
    best_stop = -1
    for s, w in targets:
        if earliest[s] + w < best_arr:
            best_arr, best_stop = earliest[s] + w, s

    start = bisect.bisect_left(tt.dep_times, t0)
    conns = tt.connections
    for ci in range(start, len(conns)):
        dep_stop, arr_stop, dep, arr, trip, flags = conns[ci]
        if dep >= best_arr:
            break
        if trip not in active:
            continue
        if trip not in trip_board:
            if not flags & 1:  # no boarding at this stop
                continue
            ready = earliest[dep_stop]
            if reached_by[dep_stop] is not None and reached_by[dep_stop][0] == "bus":
                ready += MIN_TRANSFER_SEC
            if ready > dep:
                continue
            trip_board[trip] = ci
        if flags & 2 and arr < earliest[arr_stop]:  # alighting allowed
            earliest[arr_stop] = arr
            reached_by[arr_stop] = ("bus", trip_board[trip], ci)
            if arr_stop in target_walk and arr + target_walk[arr_stop] < best_arr:
                best_arr, best_stop = arr + target_walk[arr_stop], arr_stop
            for other, walk in tt.footpaths.get(arr_stop, ()):
                t = arr + walk + WALK_PENALTY_SEC
                if t < earliest[other]:
                    earliest[other] = t
                    reached_by[other] = ("walk", arr_stop, walk)
                    if other in target_walk and t + target_walk[other] < best_arr:
                        best_arr, best_stop = t + target_walk[other], other

    if best_stop < 0 or best_arr >= INF:
        return None
    return _reconstruct(tt, best_stop, earliest, reached_by, origin_walk, target_walk, t0)


def _reconstruct(tt, stop, earliest, reached_by, origin_walk, target_walk, t0) -> Journey | None:
    # Leg times come from the actual bus times plus walking time; earliest[] may
    # include WALK_PENALTY_SEC and is only used to choose the route.
    legs: list[Leg] = []
    final_walk = target_walk.get(stop, 0)
    cur = stop
    guard = 0
    while reached_by[cur] is not None:
        guard += 1
        if guard > 200:
            return None
        how = reached_by[cur]
        if how[0] == "bus":
            board = tt.connections[how[1]]
            alight = tt.connections[how[2]]
            legs.append(Leg("bus", board[0], alight[1], board[2], alight[3], board[4]))
            cur = board[0]
        else:
            _, prev, walk = how
            legs.append(Leg("walk", prev, cur, 0, walk))  # times filled in below
            cur = prev
    if not any(leg.kind == "bus" for leg in legs):
        return None  # walking only — not a bus route
    w = origin_walk.get(cur, 0)
    if w:
        first_bus_dep = next(leg.dep_sec for leg in reversed(legs) if leg.kind == "bus")
        legs.append(Leg("walk", None, cur, first_bus_dep - w, first_bus_dep))
    legs.reverse()
    # Walking between stops starts when the previous bus arrives.
    for i, leg in enumerate(legs):
        if leg.kind == "walk" and leg.from_stop is not None and i > 0:
            walk = leg.arr_sec - leg.dep_sec
            leg.dep_sec = legs[i - 1].arr_sec
            leg.arr_sec = leg.dep_sec + walk
    if final_walk:
        legs.append(Leg("walk", stop, None, legs[-1].arr_sec, legs[-1].arr_sec + final_walk))
    return Journey(legs)


def depart_after(tt, sources, targets, t0, active, count: int = 3) -> list[Journey]:
    """Up to `count` journeys departing at or after t0 (each next one leaves later)."""
    results: list[Journey] = []
    t = t0
    for _ in range(count * 3):
        j = earliest_arrival(tt, sources, targets, t, active)
        if j is None:
            break
        if not results or j.arr_sec > results[-1].arr_sec:
            results.append(j)
            if len(results) >= count:
                break
        first_bus = next(leg for leg in j.legs if leg.kind == "bus")
        t = first_bus.dep_sec - _origin_walk(j) + 60
    return results


def arrive_by(tt, sources, targets, deadline: int, active, count: int = 2, window_sec: int = 24 * 3600) -> list[Journey]:
    """Journeys arriving by `deadline`, latest departures first."""
    candidates: list[Journey] = []
    t = max(0, deadline - window_sec)
    for _ in range(60):
        j = earliest_arrival(tt, sources, targets, t, active)
        if j is None or j.arr_sec > deadline:
            break
        candidates.append(j)
        first_bus = next(leg for leg in j.legs if leg.kind == "bus")
        t = first_bus.dep_sec - _origin_walk(j) + 60
    # Keep the latest-departing journey per arrival time, then the latest `count`.
    candidates.sort(key=lambda j: j.dep_sec, reverse=True)
    picked: list[Journey] = []
    for j in candidates:
        if all(j.arr_sec != p.arr_sec for p in picked):
            picked.append(j)
        if len(picked) >= count:
            break
    return picked


def _origin_walk(j: Journey) -> int:
    first = j.legs[0]
    return first.arr_sec - first.dep_sec if first.kind == "walk" and first.from_stop is None else 0
