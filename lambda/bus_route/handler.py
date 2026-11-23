"""genai-ai-bus: bus route search ExApp for 源内 Web (synchronous API).

Request:  {"inputs": {"question": "...", "conversation_history": "..."}}
Response: {"outputs": "<Markdown>"}
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import boto3

import csa
import geo
import llm
from timetable import Timetable

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

JST = timezone(timedelta(hours=9))
# 源内 Web cuts synchronous calls at 29 s. If we are past this budget before the
# second inference, answer with the deterministic formatter instead.
ANSWER_BUDGET_SEC = float(os.environ.get("ANSWER_BUDGET_SEC", "18"))

_timetable: Timetable | None = None


def _load_timetable() -> Timetable:
    global _timetable
    if _timetable is None:
        obj = boto3.client("s3").get_object(Bucket=os.environ["DATA_BUCKET"], Key=os.environ["INDEX_KEY"])
        _timetable = Timetable.from_bytes(obj["Body"].read())
        logger.info("timetable loaded: %s", json.dumps(_timetable.meta.get("counts"), ensure_ascii=False))
    return _timetable


def _response(status: int, outputs: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"outputs": outputs}, ensure_ascii=False),
    }


def _hhmm(sec: int) -> str:
    h, m = divmod(sec // 60, 60)
    return f"{h:02d}:{m:02d}"


def _parse_hhmm(value: str) -> int:
    h, m = value.strip().split(":")[:2]
    return int(h) * 3600 + int(m) * 60


_PLACEHOLDERS = {"", "unknown", "<unknown>", "不明", "なし", "未指定", "null", "none", "n/a", "-", "?"}


def _is_placeholder(value) -> bool:
    return str(value or "").strip().strip("「」\"'").lower() in _PLACEHOLDERS


def _bias() -> tuple[float | None, float | None]:
    raw = os.environ.get("BIAS_POSITION", "")  # "lon,lat"
    if not raw:
        return None, None
    lon, lat = (float(x) for x in raw.split(","))
    return lon, lat


def _resolve_place(tt: Timetable, text: str) -> tuple[list[tuple[int, int]], str]:
    """Return ([(stop_idx, walk_sec)], how-it-was-resolved) for a place."""
    stops = tt.find_stops_by_name(text)
    if stops:
        return [(s, 0) for s in stops], f"バス停「{tt.stops[stops[0]].name}」"
    lon, lat = _bias()
    hit = geo.geocode(text, lon, lat)
    if hit is None:
        return [], ""
    lat, lon, label = hit
    near = tt.stops_near(lat, lon)
    return near, f"「{label}」付近（座標から周辺のバス停を検索）"


def _journey_to_dict(tt: Timetable, j: csa.Journey) -> dict:
    legs = []
    for leg in j.legs:
        if leg.kind == "bus":
            trip = tt.trips[leg.trip_idx]
            legs.append({
                "type": "バス",
                "route": trip.route_name,
                "headsign": trip.headsign,
                "board": tt.stops[leg.from_stop].name,
                "depart": _hhmm(leg.dep_sec),
                "alight": tt.stops[leg.to_stop].name,
                "arrive": _hhmm(leg.arr_sec),
            })
        else:
            frm = tt.stops[leg.from_stop].name if leg.from_stop is not None else "出発地"
            to = tt.stops[leg.to_stop].name if leg.to_stop is not None else "目的地"
            legs.append({"type": "徒歩", "from": frm, "to": to, "minutes": max(1, round((leg.arr_sec - leg.dep_sec) / 60))})
    return {
        "depart": _hhmm(j.dep_sec),
        "arrive": _hhmm(j.arr_sec),
        "minutes": round((j.arr_sec - j.dep_sec) / 60),
        "transfers": j.transfers,
        "legs": legs,
    }


_WEEKDAYS_JA = "月火水木金土日"


def _day_label(d: date) -> str:
    return f"{d.month}月{d.day}日（{_WEEKDAYS_JA[d.weekday()]}）"


def _no_service_message(tt: Timetable, day: date) -> str:
    """Explain that no bus runs on `day` and point to the next operating day."""
    next_day = next((day + timedelta(days=i) for i in range(1, 15) if tt.active_trips(day + timedelta(days=i))), None)
    msg = f"{_day_label(day)}はバスの運行がありません（土日祝日・年末年始など）。"
    if next_day:
        msg += f"次に運行するのは{_day_label(next_day)}です。日付を指定してもう一度お試しください。"
    return msg


def _format_routes(routes: list[dict], day: date) -> str:
    """Deterministic Markdown (used when the answer inference is skipped or fails)."""
    lines = [f"## バス経路（{day.strftime('%Y-%m-%d')}）", ""]
    for i, r in enumerate(routes, 1):
        lines.append(f"### 候補{i}: {r['depart']} 発 → {r['arrive']} 着（{r['minutes']}分・乗り換え{r['transfers']}回）")
        for leg in r["legs"]:
            if leg["type"] == "バス":
                name = " ".join(x for x in (leg["route"], f"{leg['headsign']}行き" if leg["headsign"] else "") if x)
                lines.append(f"- 🚌 {leg['depart']} {leg['board']} → {leg['arrive']} {leg['alight']}（{name}）")
            else:
                lines.append(f"- 🚶 徒歩 約{leg['minutes']}分（{leg['from']} → {leg['to']}）")
        lines.append("")
    lines.append("※ 時刻は時刻表データに基づく予定です。遅れや運休の可能性があります。")
    return "\n".join(lines)


def lambda_handler(event, context):
    started = time.monotonic()
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, "リクエストの形式が正しくありません。")
    inputs = body.get("inputs") or {}
    question = str(inputs.get("question") or "").strip()
    history = str(inputs.get("conversation_history") or "").strip()
    if not question:
        return _response(400, "検索したい経路を入力してください。")

    region_name = os.environ.get("REGION_NAME", "")
    try:
        tt = _load_timetable()
        now = datetime.now(JST)

        # 1) Inference 1: read the search conditions
        cond = llm.parse_conditions(question, history, now, region_name)
        logger.info("conditions: %s", json.dumps(cond, ensure_ascii=False))
        # The model sometimes writes a placeholder instead of an empty string.
        for key in ("origin", "destination"):
            if _is_placeholder(cond.get(key)):
                cond[key] = ""
        # Ask back only when a place is missing; a missing date/time defaults to now.
        if not cond.get("origin") or not cond.get("destination"):
            return _response(
                200,
                cond.get("missing") or "出発地と到着地を教えてください。（例：「都農駅から都農町立病院まで、10時に着きたい」）",
            )

        try:
            day = date.fromisoformat(cond.get("date") or now.date().isoformat())
        except ValueError:
            day = now.date()
        try:
            t = _parse_hhmm(cond.get("time") or now.strftime("%H:%M"))
        except ValueError:
            t = now.hour * 3600 + now.minute * 60

        # 2) Resolve places: stop name match, else Amazon Location Service geocoding
        sources, origin_how = _resolve_place(tt, cond["origin"])
        targets, dest_how = _resolve_place(tt, cond["destination"])
        if not sources:
            return _response(200, f"出発地「{cond['origin']}」の近くにバス停が見つかりませんでした。バス停名や施設名で入力し直してください。")
        if not targets:
            return _response(200, f"到着地「{cond['destination']}」の近くにバス停が見つかりませんでした。バス停名や施設名で入力し直してください。")

        # 3) CSA route search
        active = tt.active_trips(day)
        if not active:
            return _response(200, _no_service_message(tt, day))
        if cond.get("time_type") == "arrive":
            journeys = csa.arrive_by(tt, sources, targets, t, active)
        else:
            journeys = csa.depart_after(tt, sources, targets, t, active)
        logger.info("csa: %d journeys, %.2fs elapsed", len(journeys), time.monotonic() - started)
        if not journeys:
            when = "到着" if cond.get("time_type") == "arrive" else "出発"
            return _response(
                200,
                f"{day.strftime('%Y-%m-%d')} {_hhmm(t)} {when}の条件で、{origin_how}から{dest_how}へのバス経路は見つかりませんでした。"
                "時刻や日付を変えてお試しください。",
            )
        routes = [_journey_to_dict(tt, j) for j in journeys]
        cond_out = {**cond, "origin_resolved": origin_how, "destination_resolved": dest_how}

        # 4) Inference 2: write the answer (skip if the time budget is used up)
        if time.monotonic() - started > ANSWER_BUDGET_SEC:
            logger.warning("answer inference skipped (time budget)")
            return _response(200, _format_routes(routes, day))
        try:
            return _response(200, llm.write_answer(question, cond_out, routes, region_name))
        except Exception:  # noqa: BLE001 - fall back to a plain answer
            logger.exception("answer inference failed")
            return _response(200, _format_routes(routes, day))
    except Exception:  # noqa: BLE001
        logger.exception("unhandled error")
        return _response(500, "経路検索中にエラーが発生しました。しばらくしてからもう一度お試しください。")
