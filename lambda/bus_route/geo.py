"""Amazon Location Service (Places API v2) geocoding."""

from __future__ import annotations

import os

import boto3
from botocore.config import Config

_client = None


def _places():
    global _client
    if _client is None:
        _client = boto3.client(
            "geo-places",
            region_name=os.environ.get("LOCATION_REGION", os.environ.get("AWS_REGION", "ap-northeast-1")),
            config=Config(read_timeout=5, connect_timeout=3, retries={"max_attempts": 2}),
        )
    return _client


def geocode(text: str, bias_lon: float | None, bias_lat: float | None) -> tuple[float, float, str] | None:
    """Return (lat, lon, label) for a place name or address, or None."""
    params = {
        "QueryText": text,
        "MaxResults": 1,
        "Language": "ja",
        "Filter": {"IncludeCountries": ["JPN"]},
    }
    if bias_lon is not None and bias_lat is not None:
        params["BiasPosition"] = [bias_lon, bias_lat]
    items = _places().geocode(**params).get("ResultItems", [])
    if not items:
        return None
    lon, lat = items[0]["Position"]
    return lat, lon, items[0].get("Title", text)
