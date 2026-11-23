"""Bedrock calls: (1) read search conditions, (2) write the answer."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import boto3
from botocore.config import Config

_client = None


def _bedrock():
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("MODEL_REGION", "ap-northeast-1"),
            config=Config(read_timeout=20, connect_timeout=3, retries={"max_attempts": 2}),
        )
    return _client


PARSE_TOOL = {
    "toolSpec": {
        "name": "set_search_conditions",
        "description": "バス経路検索の条件を設定する",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "出発地（バス停名・施設名・住所）。不明なら空文字"},
                    "destination": {"type": "string", "description": "到着地（バス停名・施設名・住所）。不明なら空文字"},
                    "date": {"type": "string", "description": "利用日 YYYY-MM-DD"},
                    "time": {"type": "string", "description": "時刻 HH:MM（24時間制）"},
                    "time_type": {
                        "type": "string",
                        "enum": ["depart", "arrive"],
                        "description": "time が出発時刻なら depart、到着したい時刻なら arrive",
                    },
                    "missing": {
                        "type": "string",
                        "description": "出発地か到着地が読み取れないときだけ、利用者への確認文。日付・時刻が無いだけなら補って空文字にする",
                    },
                },
                "required": ["origin", "destination", "date", "time", "time_type", "missing"],
            }
        },
    }
}


_WEEKDAYS_JA = "月火水木金土日"


def _calendar(today: date, days: int = 21) -> str:
    """Date list with weekdays: models are unreliable at weekday arithmetic."""
    rows = []
    for i in range(days):
        d = today + timedelta(days=i)
        label = "今日" if i == 0 else "明日" if i == 1 else "明後日" if i == 2 else ""
        rows.append(f"{d.isoformat()}（{_WEEKDAYS_JA[d.weekday()]}）{label}")
    return "\n".join(rows)


def parse_conditions(question: str, history: str, now: datetime, region_name: str) -> dict:
    """Inference 1: extract search conditions from natural language."""
    now_iso = f"{now.strftime('%Y-%m-%d %H:%M')}（{_WEEKDAYS_JA[now.weekday()]}）"
    system = (
        f"あなたは{region_name or '地域'}のバス経路検索の受付係です。利用者の文章から検索条件を読み取り、"
        "必ず set_search_conditions ツールで返してください。"
        f"現在日時は {now_iso}（日本時間）です。「明日」「今から」「夕方」などは現在日時を基準に具体的な日付・時刻にしてください。"
        "曜日を含む指定（「来週の火曜日」「今度の金曜」など）は、自分で曜日を計算せず、次のカレンダーから日付を選んでください。"
        "週は月曜始まりで、「来週」は次の月曜日から日曜日までです。\n"
        f"【カレンダー】\n{_calendar(now.date())}\n"
        "時刻の指定がなければ現在時刻の出発とします。"
        "会話履歴がある場合は、前回の条件を引き継ぎ、今回の文章で変わった部分だけを更新してください。"
    )
    content = question if not history else f"【会話履歴】\n{history}\n\n【今回の問い合わせ】\n{question}"
    resp = _bedrock().converse(
        modelId=os.environ["PARSE_MODEL_ID"],
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": content}]}],
        toolConfig={"tools": [PARSE_TOOL], "toolChoice": {"tool": {"name": "set_search_conditions"}}},
        inferenceConfig={"maxTokens": 400, "temperature": 0},
    )
    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            return block["toolUse"]["input"]
    raise ValueError("検索条件を読み取れませんでした")


def write_answer(question: str, conditions: dict, routes: list[dict], region_name: str) -> str:
    """Inference 2: turn computed routes into a friendly Markdown answer."""
    system = (
        f"あなたは{region_name or '地域'}のバス案内係です。経路検索の計算結果（JSON）だけをもとに、"
        "利用者に分かりやすい日本語の Markdown で回答してください。"
        "計算結果にないバス・時刻・停留所を作ってはいけません。"
        "最初に一番おすすめの経路を、乗車するバス停・系統と行き先・発車時刻・降車するバス停・到着時刻・乗り換え・徒歩の順で示し、"
        "その後に他の候補を簡潔に箇条書きしてください。最後に、時刻は時刻表データに基づく予定で、遅れや運休があり得ることを一言添えてください。"
    )
    payload = json.dumps({"question": question, "conditions": conditions, "routes": routes}, ensure_ascii=False)
    resp = _bedrock().converse(
        modelId=os.environ["ANSWER_MODEL_ID"],
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": payload}]}],
        inferenceConfig={"maxTokens": 1200, "temperature": 0.2},
    )
    return "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()
