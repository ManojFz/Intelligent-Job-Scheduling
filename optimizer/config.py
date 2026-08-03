"""Configuration and constants for the route optimizer."""

# Google Maps Distance Matrix API
GOOGLE_MAPS_API_KEY = "AIzaSyAv_5MxtaCZxTER1ldC-LnBP_q36vgjz7I"
DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

# API batching: max 10 origins x 10 destinations per request
MAX_ORIGINS_PER_REQUEST = 10
MAX_DESTINATIONS_PER_REQUEST = 10

# Work day (09:00–18:00)
SHIFT_START_TIME = "09:00"
SHIFT_END_TIME = "18:00"
OVERTIME_END_TIME = "19:00"
SHIFT_START_MINUTES = 0
SHIFT_END_MINUTES = 540
OVERTIME_END_MINUTES = 600

# Break window (minutes from shift start 09:00)
BREAK_WINDOW_START_MIN = 240   # 13:00
BREAK_WINDOW_END_MIN = 270     # 13:30
BREAK_DURATION_MIN = 30        # break node service time

# Gap between consecutive jobs (minutes) - after each ticket
DEFAULT_BREAK_DURATION_MIN = 15

# Priority time windows (minutes from shift start)
P1_MAX_START_MINUTES = 120   # P1 must start within 2 hours of shift start
P2_USE_SLA_DEADLINE = True   # normal: use sla_deadline
P3_USE_SHIFT_END = True      # relaxed: window extends to shift end

# OR-Tools
SOLVER_TIME_LIMIT_SECONDS = 30
SLA_PENALTY_PER_MINUTE = 1000
SKILL_RATING_PENALTY_KM = 10  # equivalent km for rating < 4
INELIGIBLE_CLUSTER_COST = 999_999

# Dynamic capacity: distance-spread thresholds
# (max_spread_km, max_jobs_allowed)  -- evaluated top-to-bottom, first match wins
SPREAD_THRESHOLDS: list[tuple[float, int]] = [
    (6.0, 4),     # spread <= 6 km  → allow up to 4 jobs
    (10.0, 3),    # spread <= 10 km → allow up to 3 jobs
    (18.0, 2),    # spread <= 18 km → allow up to 2 jobs
]
SPREAD_DEFAULT_LIMIT = 1  # spread > 18 km → allow only 1 job

# Relaxed thresholds for second pass (allow wider spread)
SPREAD_THRESHOLDS_RELAXED: list[tuple[float, int]] = [
    (10.0, 4),
    (20.0, 3),
    (30.0, 2),
]
SPREAD_DEFAULT_LIMIT_RELAXED = 1

# IST timezone offset (seconds) for API departure_time
IST_UTC_OFFSET_SECONDS = 5 * 3600 + 30 * 60
