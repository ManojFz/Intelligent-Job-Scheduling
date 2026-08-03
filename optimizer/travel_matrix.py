"""Travel time matrix via Google Maps Distance Matrix API with haversine fallback."""

import logging
import math
import time
from typing import Optional

import numpy as np
import requests

from optimizer.config import (
    DISTANCE_MATRIX_URL,
    GOOGLE_MAPS_API_KEY,
    MAX_DESTINATIONS_PER_REQUEST,
    MAX_ORIGINS_PER_REQUEST,
    IST_UTC_OFFSET_SECONDS,
)
from optimizer.models import Location

logger = logging.getLogger("optimizer.travel_matrix")

# Earth radius in km for haversine
EARTH_RADIUS_KM = 6371.0
# Assume average speed 30 km/h for time estimate from haversine distance
AVG_SPEED_KMH = 30.0


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in km between two points."""
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_KM * c


def haversine_time_seconds(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    """Return estimated travel time in seconds using haversine distance and average speed.

    If distance > 0, travel time must not be zero (min 60 s).
    """
    km = haversine_km(lat1, lng1, lat2, lng2)
    if km <= 0:
        return 0
    hours = km / AVG_SPEED_KMH
    sec = int(hours * 3600)
    return max(60, sec)


def build_matrix_haversine(locations: list[Location]) -> tuple[np.ndarray, np.ndarray]:
    """
    Build travel time (seconds) and distance (meters) matrices using haversine.
    Returns (time_matrix_seconds, distance_matrix_meters).
    Distance in meters for consistency with API (we'll convert to km in output).
    """
    n = len(locations)
    time_mat = np.zeros((n, n), dtype=np.int64)
    dist_mat = np.zeros((n, n), dtype=np.float64)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            km = haversine_km(
                locations[i].lat, locations[i].lng,
                locations[j].lat, locations[j].lng,
            )
            dist_mat[i, j] = km * 1000  # meters
            time_mat[i, j] = haversine_time_seconds(
                locations[i].lat, locations[i].lng,
                locations[j].lat, locations[j].lng,
            )
    return time_mat, dist_mat


def _unix_timestamp_9am_ist(date_str: str) -> int:
    """Return Unix timestamp for 09:00 on the given date (YYYY-MM-DD) in IST (UTC)."""
    from datetime import datetime, timezone, timedelta
    # 09:00 IST = 03:30 UTC (IST = UTC+5:30)
    ist = timezone(timedelta(hours=5, minutes=30))
    dt = datetime.strptime(date_str + " 09:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=ist)
    return int(dt.timestamp())


def fetch_distance_matrix_api(
    locations: list[Location],
    departure_date: str,
) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """
    Call Google Distance Matrix API in 10x10 batches.
    Returns (time_matrix_seconds, distance_matrix_meters) or None on failure.
    """
    n = len(locations)
    time_mat = np.zeros((n, n), dtype=np.int64)
    dist_mat = np.zeros((n, n), dtype=np.float64)
    dep_ts = _unix_timestamp_9am_ist(departure_date)

    for o_start in range(0, n, MAX_ORIGINS_PER_REQUEST):
        o_end = min(o_start + MAX_ORIGINS_PER_REQUEST, n)
        origins = "|".join(locations[i].to_api_string() for i in range(o_start, o_end))
        for d_start in range(0, n, MAX_DESTINATIONS_PER_REQUEST):
            d_end = min(d_start + MAX_DESTINATIONS_PER_REQUEST, n)
            destinations = "|".join(
                locations[j].to_api_string() for j in range(d_start, d_end)
            )
            params = {
                "origins": origins,
                "destinations": destinations,
                "mode": "driving",
                "departure_time": dep_ts,
                "traffic_model": "best_guess",
                "key": GOOGLE_MAPS_API_KEY,
            }
            try:
                r = requests.get(DISTANCE_MATRIX_URL, params=params, timeout=10)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                logger.warning("Google Distance Matrix request failed: %s", e)
                return None
            if data.get("status") != "OK":
                logger.warning(
                    "Google Distance Matrix status=%s error_message=%s",
                    data.get("status"),
                    data.get("error_message"),
                )
                return None
            rows = data.get("rows", [])
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                o_idx = o_start + i
                for j, elem in enumerate(row.get("elements") or []):
                    d_idx = d_start + j
                    if not isinstance(elem, dict) or elem.get("status") != "OK":
                        # Fallback for this cell
                        km = haversine_km(
                            locations[o_idx].lat, locations[o_idx].lng,
                            locations[d_idx].lat, locations[d_idx].lng,
                        )
                        dist_mat[o_idx, d_idx] = km * 1000
                        time_mat[o_idx, d_idx] = haversine_time_seconds(
                            locations[o_idx].lat, locations[o_idx].lng,
                            locations[d_idx].lat, locations[d_idx].lng,
                        )
                        continue
                    # duration_in_traffic preferred; fallback to duration
                    # Google may return null for these keys — never call .get on None
                    dur = elem.get("duration_in_traffic") or elem.get("duration") or {}
                    if not isinstance(dur, dict):
                        dur = {}
                    dist_elem = elem.get("distance") or {}
                    if not isinstance(dist_elem, dict):
                        dist_elem = {}
                    time_mat[o_idx, d_idx] = int(dur.get("value") or 0)
                    dist_mat[o_idx, d_idx] = float(dist_elem.get("value") or 0)
    return (time_mat, dist_mat)


def build_travel_matrix(
    locations: list[Location],
    departure_date: str,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Build travel time (seconds) and distance (meters) matrices.
    Uses Google API; on failure falls back to haversine silently.
    Returns (time_matrix_seconds, distance_matrix_meters).
    """
    logger.info(
        "Building travel matrix: locations=%d departure_date=%s",
        len(locations),
        departure_date,
    )
    result = fetch_distance_matrix_api(locations, departure_date)
    if result is not None:
        logger.info("Travel matrix built via Google Distance Matrix API")
        return result
    logger.info("Travel matrix using haversine fallback")
    return build_matrix_haversine(locations)
