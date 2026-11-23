"""Tests for the index builder and CSA using a tiny synthetic GTFS feed.

Network (lines A and B, transfer at 中央):
    駅前 --A--> 中央 --A--> 病院
                中央 --B--> 市役所
    市役所前東 is ~100 m from 市役所 (footpath).
"""

import io
import sys
import unittest
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lambda" / "bus_route"))
sys.path.insert(0, str(ROOT / "tools"))

import build_index  # noqa: E402
import csa  # noqa: E402
from timetable import Timetable, normalize_name  # noqa: E402

FILES = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n1,テストバス,https://example.com,Asia/Tokyo\n",
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "S1,駅前,35.0000,139.0000\n"
        "S2,中央,35.0100,139.0000\n"
        "S3,病院,35.0200,139.0000\n"
        "S4,市役所,35.0100,139.0200\n"
        "S5,市役所前東,35.0100,139.0211\n"
    ),
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nA,1,A系統,,3\nB,1,B系統,,3\n",
    "trips.txt": (
        "route_id,service_id,trip_id,trip_headsign\n"
        "A,WD,A1,病院\nA,WD,A2,病院\nB,WD,B1,市役所\nB,WD,B2,市役所\nA,HOL,A9,病院\nB,WD,C1,市役所\n"
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
        "A1,08:00:00,08:00:00,S1,1,,\nA1,08:10:00,08:10:00,S2,2,,\nA1,08:20:00,08:20:00,S3,3,,\n"
        "A2,09:00:00,09:00:00,S1,1,,\nA2,09:10:00,09:10:00,S2,2,,\nA2,09:20:00,09:20:00,S3,3,,\n"
        "B1,08:15:00,08:15:00,S2,1,,\nB1,08:30:00,08:30:00,S4,2,,\n"
        "B2,09:15:00,09:15:00,S2,1,,\nB2,09:30:00,09:30:00,S4,2,,\n"
        "A9,10:00:00,10:00:00,S1,1,,\nA9,10:10:00,10:10:00,S2,2,,\n"
        # C1 is a faster direct trip, but alighting at 市役所 is not allowed
        "C1,08:05:00,08:05:00,S1,1,0,1\nC1,08:20:00,08:20:00,S4,2,1,1\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WD,1,1,1,1,1,0,0,20260101,20271231\nHOL,0,0,0,0,0,1,1,20260101,20271231\n"
    ),
    "calendar_dates.txt": "service_id,date,exception_type\nWD,20261123,2\nHOL,20261123,1\n",
}

WEEKDAY = date(2026, 11, 20)  # Friday
HOLIDAY = date(2026, 11, 23)  # Monday, 勤労感謝の日 (WD removed, HOL added)


def hm(h, m):
    return h * 3600 + m * 60


class CsaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, text in FILES.items():
                zf.writestr(name, text)
        tmp = ROOT / "tests" / "_fixture.zip"
        tmp.write_bytes(buf.getvalue())
        try:
            cls.tt = Timetable(build_index.build(str(tmp), "テスト市"))
        finally:
            tmp.unlink()

    def stop(self, name):
        return self.tt.find_stops_by_name(name)

    def test_index_counts(self):
        self.assertEqual(len(self.tt.stops), 5)
        self.assertEqual(len(self.tt.connections), 8)

    def test_calendar_dates_override(self):
        wd = {self.tt.trips[i].trip_id for i in self.tt.active_trips(WEEKDAY)}
        hol = {self.tt.trips[i].trip_id for i in self.tt.active_trips(HOLIDAY)}
        self.assertEqual(wd, {"A1", "A2", "B1", "B2", "C1"})
        self.assertEqual(hol, {"A9"})

    def test_transfer_route(self):
        src = [(s, 0) for s in self.stop("駅前")]
        dst = [(s, 0) for s in self.stop("市役所")]
        j = csa.earliest_arrival(self.tt, src, dst, hm(7, 50), self.tt.active_trips(WEEKDAY))
        self.assertIsNotNone(j)
        self.assertEqual(j.dep_sec, hm(8, 0))
        self.assertEqual(j.arr_sec, hm(8, 30))
        self.assertEqual(j.transfers, 1)
        self.assertEqual([self.tt.trips[l.trip_idx].trip_id for l in j.legs if l.kind == "bus"], ["A1", "B1"])

    def test_depart_after_gives_next_buses(self):
        src = [(s, 0) for s in self.stop("駅前")]
        dst = [(s, 0) for s in self.stop("病院")]
        js = csa.depart_after(self.tt, src, dst, hm(7, 0), self.tt.active_trips(WEEKDAY))
        self.assertEqual([j.dep_sec for j in js], [hm(8, 0), hm(9, 0)])

    def test_arrive_by_picks_latest(self):
        src = [(s, 0) for s in self.stop("駅前")]
        dst = [(s, 0) for s in self.stop("市役所")]
        js = csa.arrive_by(self.tt, src, dst, hm(9, 40), self.tt.active_trips(WEEKDAY))
        self.assertEqual(js[0].dep_sec, hm(9, 0))
        self.assertEqual(js[0].arr_sec, hm(9, 30))

    def test_footpath_to_nearby_stop(self):
        src = [(s, 0) for s in self.stop("駅前")]
        dst = [(s, 0) for s in self.stop("市役所前東")]
        j = csa.earliest_arrival(self.tt, src, dst, hm(7, 50), self.tt.active_trips(WEEKDAY))
        self.assertIsNotNone(j)
        self.assertEqual(j.legs[-1].kind, "walk")
        self.assertGreater(j.arr_sec, hm(8, 30))

    def test_no_route_on_holiday(self):
        src = [(s, 0) for s in self.stop("駅前")]
        dst = [(s, 0) for s in self.stop("市役所")]
        self.assertIsNone(csa.earliest_arrival(self.tt, src, dst, hm(7, 0), self.tt.active_trips(HOLIDAY)))

    def test_origin_walk_from_coordinates(self):
        near = self.tt.stops_near(35.0005, 139.0, radius_m=200)
        dst = [(s, 0) for s in self.stop("病院")]
        j = csa.earliest_arrival(self.tt, near, dst, hm(7, 50), self.tt.active_trips(WEEKDAY))
        self.assertEqual(j.legs[0].kind, "walk")
        self.assertEqual(j.legs[0].arr_sec, hm(8, 0))

    def test_name_normalization(self):
        self.assertEqual(normalize_name("駅前バス停"), "駅前")
        self.assertEqual(normalize_name("ＡＢＣ　停留所"), "abc")
        self.assertEqual(self.stop("市役所"), self.stop("市役所 バス停"))


if __name__ == "__main__":
    unittest.main()
