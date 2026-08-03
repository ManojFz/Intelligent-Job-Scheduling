"""Time / shift helpers: travel before SLA, slot before SLA, arrival-based SLA.

Hard-check order (after skill / workflow / work location):
  shift time window → slot → max ticket → travel time → SLA → overtime
"""

from datetime import datetime
from typing import Optional, Tuple


def hhmm_to_minutes(s: str) -> int:
    """Minutes from midnight for 'HH:MM'."""
    parts = s.strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return h * 60 + m


def shift_span_minutes(shift_start: str, shift_end: str) -> int:
    """Length of shift in minutes."""
    return max(0, hhmm_to_minutes(shift_end) - hhmm_to_minutes(shift_start))


def _parse_sla_datetime(sla_iso: str) -> datetime:
    try:
        dt = datetime.fromisoformat(sla_iso.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.strptime(sla_iso[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo:
        dt = dt.replace(tzinfo=None)
    return dt


def sla_deadline_minutes_from_shift_start(
    sla_iso: Optional[str],
    solve_date: str,
    shift_start_hhmm: str = "09:00",
) -> float:
    """Minutes from shift start to SLA deadline; infinity when no SLA exists."""
    if not sla_iso:
        return float("inf")
    dt_sla = _parse_sla_datetime(sla_iso)
    base = datetime.strptime(
        solve_date[:10] + " " + shift_start_hhmm.strip() + ":00",
        "%Y-%m-%d %H:%M:%S",
    )
    delta = dt_sla - base
    return float(delta.total_seconds() / 60.0)


def travel_time_minutes_spec(dist_km: float, speed_kmph: float) -> float:
    """If travel_speed_kmph is used: travel_time_min = (distance_km / speed) * 60.

    If distance_km > 0, travel time must be > 0.
    """
    if dist_km <= 0:
        return 0.0
    return max(1e-3, (dist_km / max(speed_kmph, 1e-6)) * 60.0)


def slot_start_minutes_from_shift_start(slot_label: str, shift_start_hhmm: str) -> Optional[float]:
    """Parse label like '08-10' → start hour; minutes from shift start (not negative clamp)."""
    try:
        start_h = int(str(slot_label).split("-")[0].strip())
    except (ValueError, IndexError):
        return None
    slot_at_midnight = float(start_h * 60)
    ss = float(hhmm_to_minutes(shift_start_hhmm))
    return slot_at_midnight - ss


def compute_timeline_to_candidate(
    engineer_id: str,
    shift_start_hhmm: str,
    prior_jobs_in_order: list,
    candidate,
    slot_index_for_candidate: int,
    available_slots: Optional[list],
    location_index: dict,
    travel_distance_meters,
    break_gap_min: int,
    speed_kmph: float,
) -> Tuple[float, float, float, float]:
    """Return (total_travel_min, arrival, job_start, job_end) in minutes from shift start.

    * Travel time is computed **before** SLA (formula only when speed is set).
    * *arrival* = previous_job_end + travel_time.
    * *job_start* = max(arrival, slot_start) when slot labels exist.
    * *job_end* = job_start + estimated_duration.
    """
    eng_idx = location_index.get(engineer_id)
    cand_idx = location_index.get(candidate.job_id)
    if eng_idx is None or cand_idx is None:
        return float("inf"), float("inf"), float("inf"), float("inf")

    total_travel_min = 0.0
    prev_end = 0.0  # minutes from shift start: end time of previous work
    prev_idx = eng_idx

    for idx, j in enumerate(prior_jobs_in_order):
        j_idx = location_index.get(j.job_id)
        if j_idx is None:
            continue
        d_km = travel_distance_meters[prev_idx, j_idx] / 1000.0
        leg = travel_time_minutes_spec(d_km, speed_kmph)
        total_travel_min += leg
        arrival_j = prev_end + leg

        job_start_j = arrival_j
        if available_slots and idx < len(available_slots):
            ss = slot_start_minutes_from_shift_start(available_slots[idx], shift_start_hhmm)
            if ss is not None:
                job_start_j = max(arrival_j, ss)

        job_end_j = job_start_j + float(j.estimated_duration_min)
        prev_end = job_end_j + float(break_gap_min)
        prev_idx = j_idx

    d_km = travel_distance_meters[prev_idx, cand_idx] / 1000.0
    leg = travel_time_minutes_spec(d_km, speed_kmph)
    total_travel_min += leg
    arrival = prev_end + leg

    job_start = arrival
    if available_slots and slot_index_for_candidate < len(available_slots):
        ss = slot_start_minutes_from_shift_start(
            available_slots[slot_index_for_candidate], shift_start_hhmm
        )
        if ss is not None:
            job_start = max(arrival, ss)

    job_end = job_start + float(candidate.estimated_duration_min)
    return total_travel_min, arrival, job_start, job_end


def max_route_minutes_allowed(shift_start: str, shift_end: str, overtime_allowed: bool, cfg) -> float:
    """Upper bound on job_end (minutes from shift start) for hard overtime check."""
    base = float(shift_span_minutes(shift_start, shift_end))
    if cfg is None:
        return base + (60.0 if overtime_allowed else 0.0)

    if not cfg.overtime.enabled:
        return base + 60.0

    if cfg.overtime.is_hard:
        return base + (60.0 if overtime_allowed else 0.0)
    return base + 60.0


# Backwards compatibility for clustering imports
def estimate_job_completion_minutes_from_shift_start(
    engineer_id: str,
    prior_jobs_in_order: list,
    candidate,
    location_index: dict,
    travel_distance_meters,
    travel_time_seconds,
    break_gap_min: int,
    speed_kmph: float,
    shift_start_hhmm: str = "09:00",
    available_slots: Optional[list] = None,
    slot_index: int = 0,
) -> tuple[float, float]:
    """Return (arrival_min, job_end_min) — travel from speed×distance only; SLA uses same timeline."""
    del travel_time_seconds  # spec: use travel_speed_kmph × distance only
    _, arrival, _, job_end = compute_timeline_to_candidate(
        engineer_id,
        shift_start_hhmm,
        prior_jobs_in_order,
        candidate,
        slot_index,
        available_slots,
        location_index,
        travel_distance_meters,
        break_gap_min,
        speed_kmph,
    )
    return arrival, job_end


def passes_shift_and_sla_after_travel(
    eng,
    job,
    current_jobs: list,
    location_index: dict,
    travel_distance_meters,
    solve_date: str,
    break_duration_min: int,
    speed_kmph: float,
    cfg,
) -> tuple[bool, float, float, float]:
    """Order: travel computed → arrival/job_end → shift end → SLA (hard) → overtime (hard)."""
    slots = eng.available_slots if eng.slot_based else None
    slot_i = len(current_jobs)
    _total_t, _arrival, _job_start, job_end = compute_timeline_to_candidate(
        eng.engineer_id,
        eng.shift_start,
        current_jobs,
        job,
        slot_i,
        slots,
        location_index,
        travel_distance_meters,
        break_duration_min,
        speed_kmph,
    )

    shift_cap = max_route_minutes_allowed(eng.shift_start, eng.shift_end, eng.overtime_allowed, cfg)
    sla_limit = sla_deadline_minutes_from_shift_start(job.sla_deadline, solve_date, eng.shift_start)

    # Shift time available: job_end within allowed window (shift + optional overtime)
    if job_end > shift_cap:
        return False, job_end, sla_limit, shift_cap

    # SLA (hard): only after arrival/job_end known (travel computed before this)
    if cfg is None or (cfg.sla.enabled and cfg.sla.is_hard):
        if job_end > sla_limit:
            return False, job_end, sla_limit, shift_cap

    return True, job_end, sla_limit, shift_cap
