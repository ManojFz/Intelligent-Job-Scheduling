"""Job-to-engineer assignment with dynamic travel-spread capacity.

Assignment strategy
-------------------
1. Config-driven hard/soft constraint evaluation
2. Nearest-first per priority (P1 → P2 → P3), round-robin across engineers
3. Hungarian batch for remaining jobs  (spread-validated after)
4. Greedy fill with spread check
5. Rebalance to cut travel  (spread-aware)
6. Final greedy retry
7. Relaxed multi-pass: drop SOFT constraints, widen spread thresholds,
   repeat until no improvement — maximises assigned jobs.

Constraint config rules
-----------------------
* Disabled  → constraint is completely ignored
* Enabled + Hard → must pass; job NOT assigned if it fails
* Enabled + Soft → try to satisfy; violation adds a cost penalty but
                    does NOT block assignment

Dynamic capacity
----------------
Each engineer's actual job count is limited by::

    effective_capacity = min(slot_count, spread_limit, max_jobs_per_shift)

*slot_count* comes from ``availableSlots`` / ``slots`` in the payload,
*max_jobs_per_shift* from the engineer definition, and *spread_limit* is
recalculated after every assignment from the maximum pairwise distance
among all jobs (+ base) assigned to the engineer.
"""

import math
from datetime import datetime
from typing import Optional

import numpy as np
from scipy.optimize import linear_sum_assignment

from optimizer.config import (
    INELIGIBLE_CLUSTER_COST,
    SPREAD_DEFAULT_LIMIT,
    SPREAD_DEFAULT_LIMIT_RELAXED,
    SPREAD_THRESHOLDS,
    SPREAD_THRESHOLDS_RELAXED,
)
from optimizer.models import ConstraintConfig, Engineer, Job, SolverParams, UnassignedJob
from optimizer.scoring import (
    choose_engineer_with_preference,
    compute_soft_score,
    total_assignment_score,
)
from optimizer.time_utils import (
    passes_shift_and_sla_after_travel,
    travel_time_minutes_spec,
)

# ---------------------------------------------------------------------------
# Low-level predicate helpers (no config awareness)
# ---------------------------------------------------------------------------


def _engineer_has_all_skills(engineer: Engineer, required_skills: list[str]) -> bool:
    return all(s in engineer.skills for s in required_skills)


def _engineer_matches_workflow(engineer: Engineer, workflow_type: str) -> bool:
    if not getattr(engineer, "workflows", None):
        return True
    return workflow_type in engineer.workflows


def _engineer_matches_location(engineer: Engineer, job_location_name: Optional[str]) -> bool:
    if not getattr(engineer, "locations", None):
        return True
    if not job_location_name:
        return True
    job_area = job_location_name.strip().lower()
    return any(loc and loc.strip().lower() == job_area for loc in engineer.locations)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return 6371.0 * c


# ---------------------------------------------------------------------------
# Config-aware eligibility
# ---------------------------------------------------------------------------


def is_hard_eligible(engineer: Engineer, job: Job, cfg: Optional[ConstraintConfig] = None) -> bool:
    """Return True only if all HARD+ENABLED constraints pass.

    When *cfg* is None the original behaviour is preserved:
    skill + workflow are hard, location is also treated as hard in Phase 1.
    """
    if cfg is None:
        return (
            _engineer_has_all_skills(engineer, job.required_skills)
            and _engineer_matches_workflow(engineer, job.workflow_type)
            and _engineer_matches_location(engineer, getattr(job, "location_name", None))
        )

    if cfg.skill_match.is_hard:
        if not _engineer_has_all_skills(engineer, job.required_skills):
            return False

    if cfg.workflow.is_hard:
        if not _engineer_matches_workflow(engineer, job.workflow_type):
            return False

    if cfg.worklocation.is_hard:
        if not _engineer_matches_location(engineer, getattr(job, "location_name", None)):
            return False

    return True


def _is_skill_workflow_eligible(engineer: Engineer, job: Job, cfg: Optional[ConstraintConfig] = None) -> bool:
    """Relaxed check used in Phase 2: only HARD constraints are evaluated.

    SOFT constraints are intentionally skipped so that unassigned jobs get
    a second chance to be placed (with a cost penalty).  If a constraint is
    Hard it is NEVER relaxed, regardless of phase.

    Backward-compat (cfg=None): skill + workflow only, location dropped.
    """
    if cfg is None:
        return (
            _engineer_has_all_skills(engineer, job.required_skills)
            and _engineer_matches_workflow(engineer, job.workflow_type)
        )

    if cfg.skill_match.is_hard:
        if not _engineer_has_all_skills(engineer, job.required_skills):
            return False

    if cfg.workflow.is_hard:
        if not _engineer_matches_workflow(engineer, job.workflow_type):
            return False

    # worklocation: only enforce if HARD.  Soft → dropped in Phase 2.
    if cfg.worklocation.is_hard:
        if not _engineer_matches_location(engineer, getattr(job, "location_name", None)):
            return False

    return True


def job_to_eligible_engineers(job: Job, engineers: list[Engineer], cfg: Optional[ConstraintConfig] = None) -> list[Engineer]:
    return [e for e in engineers if is_hard_eligible(e, job, cfg)]


# ---------------------------------------------------------------------------
# Hard operational checks (order: shift/slot capacity, SLA after travel, overtime)
# ---------------------------------------------------------------------------


def _travel_leg_km(
    eng: Engineer,
    current_jobs: list[Job],
    job: Job,
    location_index: dict,
    travel_distance_meters: np.ndarray,
) -> float:
    if not current_jobs:
        prev_idx = location_index.get(eng.engineer_id)
    else:
        prev_idx = location_index.get(current_jobs[-1].job_id)
    j_idx = location_index.get(job.job_id)
    if prev_idx is None or j_idx is None:
        return 99999.0
    return float(travel_distance_meters[prev_idx, j_idx] / 1000.0)


def _dist_base_to_job_km(
    eng: Engineer,
    job: Job,
    location_index: dict,
    travel_distance_meters: np.ndarray,
) -> float:
    ei = location_index.get(eng.engineer_id)
    ji = location_index.get(job.job_id)
    if ei is None or ji is None:
        return 99999.0
    return float(travel_distance_meters[ei, ji] / 1000.0)


def _passes_hard_operational(
    eng: Engineer,
    job: Job,
    current_jobs: list[Job],
    location_index: dict,
    travel_distance_meters: np.ndarray,
    travel_time_seconds: np.ndarray,
    cfg: Optional[ConstraintConfig],
    solve_date: str,
    break_duration_min: int,
    solver_params: SolverParams,
    *,
    effective_max_jobs: Optional[int] = None,
) -> bool:
    """Hard order after skill/workflow/location: slot → max ticket → travel → shift → SLA → overtime.

    Travel time uses (distance_km / travel_speed_kmph) * 60 only; SLA only after job_end is known.
    """
    _ = travel_time_seconds  # matrix time not used for hard feasibility (spec: speed × distance)

    # 5 Slot available (before SLA / travel timeline)
    if eng.slot_based:
        slots = eng.available_slots or []
        if len(slots) > 0 and len(current_jobs) >= len(slots):
            return False

    # 6 Max ticket per shift
    proposed = len(current_jobs) + 1
    cap = effective_max_jobs if effective_max_jobs is not None else eng.max_jobs_per_shift
    if cfg is None or cfg.max_ticket_per_day.is_hard or cfg.max_ticket_per_day.is_soft:
        if proposed > cap:
            return False

    # 7–9 Travel time → job_end → shift window → SLA (if hard) → overtime (in shift_cap)
    if not solve_date:
        return True

    ok, _, _, _ = passes_shift_and_sla_after_travel(
        eng, job, current_jobs, location_index, travel_distance_meters,
        solve_date, break_duration_min, solver_params.travel_speed_kmph, cfg,
    )
    return ok


# ---------------------------------------------------------------------------
# Dynamic spread capacity
# ---------------------------------------------------------------------------


def _compute_spread_km(
    eng_id: str,
    jobs: list[Job],
    location_index: dict,
    travel_distance_meters: np.ndarray,
) -> float:
    """Max distance between eng base and any job, or between any two jobs."""
    if not jobs:
        return 0.0
    eng_idx = location_index.get(eng_id)
    if eng_idx is None:
        return 0.0
    indices = [eng_idx] + [
        location_index[j.job_id] for j in jobs if j.job_id in location_index
    ]
    max_d = 0.0
    for i, a in enumerate(indices):
        for b in indices[i + 1:]:
            d = travel_distance_meters[a, b] / 1000.0
            if d > max_d:
                max_d = d
    return max_d


def _spread_to_dynamic_limit(spread_km: float) -> int:
    for threshold_km, limit in SPREAD_THRESHOLDS:
        if spread_km <= threshold_km:
            return limit
    return SPREAD_DEFAULT_LIMIT


def _can_add_job(
    eng: Engineer,
    current_jobs: list[Job],
    candidate: Job,
    location_index: dict,
    travel_distance_meters: np.ndarray,
    travel_time_seconds: np.ndarray,
    cfg: Optional[ConstraintConfig] = None,
    solve_date: str = "",
    break_duration_min: int = 15,
    solver_params: Optional[SolverParams] = None,
) -> bool:
    """Ticket + spread + operational hard checks (SLA after travel, slot, overtime)."""
    sp = solver_params or SolverParams()
    if solve_date and not _passes_hard_operational(
        eng, candidate, current_jobs, location_index,
        travel_distance_meters, travel_time_seconds, cfg,
        solve_date, break_duration_min, sp,
    ):
        return False

    proposed_count = len(current_jobs) + 1

    spread = _compute_spread_km(
        eng.engineer_id, current_jobs + [candidate], location_index, travel_distance_meters,
    )
    dyn_limit = _spread_to_dynamic_limit(spread)

    ticket_cap = eng.max_jobs_per_shift if (cfg is None or cfg.max_ticket_per_day.enabled) else 9999
    return proposed_count <= min(ticket_cap, dyn_limit)


# ---------------------------------------------------------------------------
# Relaxed capacity (Phase 2)
# ---------------------------------------------------------------------------


def _spread_to_dynamic_limit_relaxed(spread_km: float) -> int:
    for threshold_km, limit in SPREAD_THRESHOLDS_RELAXED:
        if spread_km <= threshold_km:
            return limit
    return SPREAD_DEFAULT_LIMIT_RELAXED


def _can_add_job_relaxed(
    eng: Engineer,
    current_jobs: list[Job],
    candidate: Job,
    location_index: dict,
    travel_distance_meters: np.ndarray,
    travel_time_seconds: np.ndarray,
    cfg: Optional[ConstraintConfig] = None,
    solve_date: str = "",
    break_duration_min: int = 15,
    solver_params: Optional[SolverParams] = None,
) -> bool:
    """Like ``_can_add_job`` but with wider spread thresholds + relaxed ticket cap."""
    sp = solver_params or SolverParams()

    if cfg is not None and not cfg.max_ticket_per_day.enabled:
        ticket_cap = 9999  # Disabled → no limit
    elif cfg is not None and cfg.max_ticket_per_day.is_soft:
        ticket_cap = eng.max_jobs_per_shift + 2  # Soft → slight overflow allowed
    else:
        ticket_cap = eng.max_jobs_per_shift  # Hard or legacy

    proposed_count = len(current_jobs) + 1
    if proposed_count > ticket_cap:
        return False

    if solve_date and not _passes_hard_operational(
        eng, candidate, current_jobs, location_index,
        travel_distance_meters, travel_time_seconds, cfg,
        solve_date, break_duration_min, sp,
        effective_max_jobs=ticket_cap,
    ):
        return False

    spread = _compute_spread_km(
        eng.engineer_id, current_jobs + [candidate], location_index, travel_distance_meters,
    )
    dyn_limit = _spread_to_dynamic_limit_relaxed(spread)
    return proposed_count <= min(ticket_cap, dyn_limit)


# ---------------------------------------------------------------------------
# Score = soft penalty (after hard checks) + travel km
# ---------------------------------------------------------------------------


def _total_score_for_assignment(
    eng: Engineer,
    job: Job,
    current_jobs: list[Job],
    location_index: dict,
    travel_distance_meters: np.ndarray,
    travel_time_seconds: np.ndarray,
    cfg: Optional[ConstraintConfig],
    solver_params: SolverParams,
) -> float:
    """Lower is better: soft scoring + travel leg km (preferred engineer applied in soft)."""
    leg_km = _travel_leg_km(eng, current_jobs, job, location_index, travel_distance_meters)
    base_km = _dist_base_to_job_km(eng, job, location_index, travel_distance_meters)

    if cfg is not None and not cfg.engineer_base_location.enabled:
        base_km = 0.0

    cluster_ok = _engineer_matches_location(eng, getattr(job, "location_name", None))
    leg_min = travel_time_minutes_spec(leg_km, solver_params.travel_speed_kmph)
    soft = compute_soft_score(
        eng,
        job,
        travel_leg_km=leg_km,
        travel_leg_min=leg_min,
        dist_base_to_job_km=base_km if base_km < 99998 else 0.0,
        cluster_radius_km=solver_params.cluster_radius_km,
        cfg=cfg,
        cluster_coherent=cluster_ok,
    )
    return total_assignment_score(soft, leg_km, leg_min)


# ---------------------------------------------------------------------------
# Pass A: nearest-first per-priority (round-robin)
# ---------------------------------------------------------------------------


def _nearest_first_pass(
    priority: Optional[str],
    engineers: list[Engineer],
    engineer_jobs: dict[str, list[Job]],
    assigned_job_ids: set[str],
    assignable: list[Job],
    job_eligible_map: dict[str, list[str]],
    location_index: dict,
    travel_distance_meters: np.ndarray,
    travel_time_seconds: np.ndarray,
    cfg: Optional[ConstraintConfig] = None,
    solve_date: str = "",
    break_duration_min: int = 15,
    solver_params: Optional[SolverParams] = None,
):
    """Round-robin: each engineer picks lowest score (soft + travel) among eligible jobs."""
    sp = solver_params or SolverParams()
    stopped: set[str] = set()
    made_progress = True
    while made_progress:
        made_progress = False
        for eng in engineers:
            eid = eng.engineer_id
            if eid in stopped:
                continue

            current = engineer_jobs.get(eid, [])
            last_idx = location_index.get(current[-1].job_id if current else eid)
            if last_idx is None:
                stopped.add(eid)
                continue

            best_job: Optional[Job] = None
            best_score = float("inf")

            for job in assignable:
                if priority is not None and job.priority != priority:
                    continue
                if job.job_id in assigned_job_ids:
                    continue
                if eid not in job_eligible_map.get(job.job_id, []):
                    continue
                j_idx = location_index.get(job.job_id)
                if j_idx is None:
                    continue
                if not _can_add_job(
                    eng, current, job, location_index, travel_distance_meters,
                    travel_time_seconds, cfg, solve_date, break_duration_min, sp,
                ):
                    continue
                sc = _total_score_for_assignment(
                    eng, job, current, location_index,
                    travel_distance_meters, travel_time_seconds, cfg, sp,
                )
                if sc < best_score:
                    best_score = sc
                    best_job = job

            if best_job is None:
                stopped.add(eid)
                continue

            engineer_jobs[eid].append(best_job)
            assigned_job_ids.add(best_job.job_id)
            made_progress = True


# ---------------------------------------------------------------------------
# Pass B: Hungarian (batch) with spread validation
# ---------------------------------------------------------------------------


def _hungarian_pass(
    jobs_pool: list[Job],
    engineers: list[Engineer],
    engineer_jobs: dict[str, list[Job]],
    assigned_job_ids: set[str],
    job_eligible_map: dict[str, list[str]],
    location_index: dict,
    travel_distance_meters: np.ndarray,
    travel_time_seconds: np.ndarray,
    cfg: Optional[ConstraintConfig] = None,
    solve_date: str = "",
    break_duration_min: int = 15,
    solver_params: Optional[SolverParams] = None,
):
    """Expanded-slot Hungarian, then trim assignments that violate spread."""
    sp = solver_params or SolverParams()
    pending = [j for j in jobs_pool if j.job_id not in assigned_job_ids]
    if not pending:
        return

    engineer_map = {e.engineer_id: e for e in engineers}
    BIG = float(INELIGIBLE_CLUSTER_COST)

    slot_to_eng: list[str] = []
    for eng in engineers:
        remaining = eng.max_jobs_per_shift - len(engineer_jobs.get(eng.engineer_id, []))
        for _ in range(max(0, remaining)):
            slot_to_eng.append(eng.engineer_id)

    if not slot_to_eng:
        return

    n_jobs = len(pending)
    n_slots = len(slot_to_eng)
    size = max(n_jobs, n_slots)
    cost = np.full((size, size), BIG)

    for ji, job in enumerate(pending):
        eligible_ids = set(job_eligible_map.get(job.job_id, []))
        for si, eng_id in enumerate(slot_to_eng):
            if eng_id not in eligible_ids:
                continue
            eng = engineer_map[eng_id]
            current = engineer_jobs.get(eng_id, [])
            cost[ji, si] = _total_score_for_assignment(
                eng, job, current, location_index,
                travel_distance_meters, travel_time_seconds, cfg, sp,
            )

    row_ind, col_ind = linear_sum_assignment(cost)

    for ji, si in zip(row_ind, col_ind):
        if ji >= n_jobs or si >= n_slots:
            continue
        if cost[ji, si] >= BIG:
            continue
        job = pending[ji]
        eng_id = slot_to_eng[si]
        eng = engineer_map[eng_id]
        current = engineer_jobs[eng_id]
        if not _can_add_job(
            eng, current, job, location_index, travel_distance_meters,
            travel_time_seconds, cfg, solve_date, break_duration_min, sp,
        ):
            continue
        engineer_jobs[eng_id].append(job)
        assigned_job_ids.add(job.job_id)

    _validate_spread(engineers, engineer_jobs, assigned_job_ids, location_index, travel_distance_meters)


def _validate_spread(
    engineers: list[Engineer],
    engineer_jobs: dict[str, list[Job]],
    assigned_job_ids: set[str],
    location_index: dict,
    travel_distance_meters: np.ndarray,
):
    """Trim jobs from over-capacity engineers (farthest first)."""
    for eng in engineers:
        eid = eng.engineer_id
        jobs_list = engineer_jobs[eid]
        eng_idx = location_index.get(eid)
        if eng_idx is None:
            continue
        while jobs_list:
            spread = _compute_spread_km(eid, jobs_list, location_index, travel_distance_meters)
            eff = min(eng.max_jobs_per_shift, _spread_to_dynamic_limit(spread))
            if len(jobs_list) <= eff:
                break
            worst_i, worst_d = 0, -1.0
            for i, j in enumerate(jobs_list):
                j_idx = location_index.get(j.job_id)
                if j_idx is None:
                    continue
                d = travel_distance_meters[eng_idx, j_idx] / 1000.0
                if d > worst_d:
                    worst_d = d
                    worst_i = i
            removed = jobs_list.pop(worst_i)
            assigned_job_ids.discard(removed.job_id)


# ---------------------------------------------------------------------------
# Pass C: greedy fill (spread-aware)
# ---------------------------------------------------------------------------


def _greedy_pass(
    jobs_pool: list[Job],
    engineers: list[Engineer],
    engineer_jobs: dict[str, list[Job]],
    assigned_job_ids: set[str],
    job_eligible_map: dict[str, list[str]],
    location_index: dict,
    travel_distance_meters: np.ndarray,
    travel_time_seconds: np.ndarray,
    cfg: Optional[ConstraintConfig] = None,
    solve_date: str = "",
    break_duration_min: int = 15,
    solver_params: Optional[SolverParams] = None,
):
    engineer_map = {e.engineer_id: e for e in engineers}
    sp = solver_params or SolverParams()
    pending = sorted(
        [j for j in jobs_pool if j.job_id not in assigned_job_ids],
        key=lambda j: (j.sla_deadline is None, j.sla_deadline or ""),
    )
    for job in pending:
        eligible_ids = job_eligible_map.get(job.job_id, [])
        scored: list[tuple[str, float]] = []
        for eng_id in eligible_ids:
            eng = engineer_map[eng_id]
            current = engineer_jobs[eng_id]
            if not _can_add_job(
                eng, current, job, location_index, travel_distance_meters,
                travel_time_seconds, cfg, solve_date, break_duration_min, sp,
            ):
                continue
            c = _total_score_for_assignment(
                eng, job, current, location_index,
                travel_distance_meters, travel_time_seconds, cfg, sp,
            )
            scored.append((eng_id, c))
        chosen = choose_engineer_with_preference(
            scored,
            job.preferred_engineer_id,
            override_gap=sp.preferred_override_gap,
        )
        if chosen is not None:
            engineer_jobs[chosen].append(job)
            assigned_job_ids.add(job.job_id)


# ---------------------------------------------------------------------------
# Pass C-relaxed: drop SOFT constraints, wider spread, slight capacity overflow
# ---------------------------------------------------------------------------


def _relaxed_greedy_pass(
    jobs_pool: list[Job],
    engineers: list[Engineer],
    engineer_jobs: dict[str, list[Job]],
    assigned_job_ids: set[str],
    location_index: dict,
    travel_distance_meters: np.ndarray,
    travel_time_seconds: np.ndarray,
    cfg: Optional[ConstraintConfig] = None,
    solve_date: str = "",
    break_duration_min: int = 15,
    solver_params: Optional[SolverParams] = None,
):
    """Greedy pass that respects only HARD constraints.

    SOFT constraints are dropped so that jobs with soft violations still get
    placed (with penalty cost already baked into the cost function).
    Spread thresholds are widened and ticket capacity may overflow slightly
    when ``max_ticket_per_day`` is Soft.
    """
    sp = solver_params or SolverParams()
    pending = sorted(
        [j for j in jobs_pool if j.job_id not in assigned_job_ids],
        key=lambda j: (j.sla_deadline is None, j.sla_deadline or ""),
    )
    for job in pending:
        scored: list[tuple[str, float]] = []
        for eng in engineers:
            if not _is_skill_workflow_eligible(eng, job, cfg):
                continue
            current = engineer_jobs[eng.engineer_id]
            if not _can_add_job_relaxed(
                eng, current, job, location_index, travel_distance_meters,
                travel_time_seconds, cfg, solve_date, break_duration_min, sp,
            ):
                continue
            c = _total_score_for_assignment(
                eng, job, current, location_index,
                travel_distance_meters, travel_time_seconds, cfg, sp,
            )
            scored.append((eng.engineer_id, c))
        chosen = choose_engineer_with_preference(
            scored,
            job.preferred_engineer_id,
            override_gap=sp.preferred_override_gap,
        )
        if chosen is not None:
            engineer_jobs[chosen].append(job)
            assigned_job_ids.add(job.job_id)


# ---------------------------------------------------------------------------
# Pass D: rebalance (spread-aware)
# ---------------------------------------------------------------------------


def _route_distance(
    eng_id: str,
    engineer_jobs: dict[str, list[Job]],
    location_index: dict,
    travel_distance_meters: np.ndarray,
) -> float:
    jobs_list = engineer_jobs.get(eng_id, [])
    if not jobs_list:
        return 0.0
    prev_idx = location_index.get(eng_id)
    if prev_idx is None:
        return 0.0
    total = 0.0
    for j in jobs_list:
        j_idx = location_index.get(j.job_id)
        if j_idx is None:
            continue
        total += travel_distance_meters[prev_idx, j_idx] / 1000.0
        prev_idx = j_idx
    return total


def _rebalance_travel(
    engineers: list[Engineer],
    engineer_jobs: dict[str, list[Job]],
    job_eligible_map: dict[str, list[str]],
    location_index: dict,
    travel_distance_meters: np.ndarray,
    travel_time_seconds: np.ndarray,
    cfg: Optional[ConstraintConfig] = None,
    solve_date: str = "",
    break_duration_min: int = 15,
    solver_params: Optional[SolverParams] = None,
):
    sp = solver_params or SolverParams()
    engineer_map = {e.engineer_id: e for e in engineers}
    eng_ids = [e.engineer_id for e in engineers]

    def _pair_cost(a: str, b: str) -> float:
        return (
            _route_distance(a, engineer_jobs, location_index, travel_distance_meters)
            + _route_distance(b, engineer_jobs, location_index, travel_distance_meters)
        )

    improved = True
    max_iters = 30
    itr = 0
    while improved and itr < max_iters:
        improved = False
        itr += 1
        for i, eng1_id in enumerate(eng_ids):
            if improved:
                break
            for eng2_id in eng_ids[i + 1:]:
                if improved:
                    break
                jobs1 = engineer_jobs[eng1_id]
                jobs2 = engineer_jobs[eng2_id]

                for j_idx in range(len(jobs1)):
                    job = jobs1[j_idx]
                    if eng2_id not in job_eligible_map.get(job.job_id, []):
                        continue
                    eng2 = engineer_map[eng2_id]
                    remaining2 = [jj for jj in jobs2]
                    if not _can_add_job(
                        eng2, remaining2, job, location_index, travel_distance_meters,
                        travel_time_seconds, cfg, solve_date, break_duration_min, sp,
                    ):
                        continue
                    old = _pair_cost(eng1_id, eng2_id)
                    jobs1.pop(j_idx)
                    jobs2.append(job)
                    if _pair_cost(eng1_id, eng2_id) < old - 0.1:
                        improved = True
                        break
                    jobs2.pop()
                    jobs1.insert(j_idx, job)

                if improved:
                    continue

                for j1i in range(len(jobs1)):
                    if improved:
                        break
                    for j2i in range(len(jobs2)):
                        job1, job2 = jobs1[j1i], jobs2[j2i]
                        if eng2_id not in job_eligible_map.get(job1.job_id, []):
                            continue
                        if eng1_id not in job_eligible_map.get(job2.job_id, []):
                            continue
                        eng1 = engineer_map[eng1_id]
                        eng2 = engineer_map[eng2_id]
                        new1 = [jj for jj in jobs1 if jj is not job1] + [job2]
                        new2 = [jj for jj in jobs2 if jj is not job2] + [job1]
                        sp1 = _compute_spread_km(eng1_id, new1, location_index, travel_distance_meters)
                        sp2 = _compute_spread_km(eng2_id, new2, location_index, travel_distance_meters)
                        if (len(new1) > min(eng1.max_jobs_per_shift, _spread_to_dynamic_limit(sp1))
                                or len(new2) > min(eng2.max_jobs_per_shift, _spread_to_dynamic_limit(sp2))):
                            continue
                        old = _pair_cost(eng1_id, eng2_id)
                        jobs1[j1i], jobs2[j2i] = job2, job1
                        if _pair_cost(eng1_id, eng2_id) < old - 0.1:
                            improved = True
                            break
                        jobs1[j1i], jobs2[j2i] = job1, job2


# ---------------------------------------------------------------------------
# Unassigned reason helper
# ---------------------------------------------------------------------------


def _unassigned_reason(job: Job, engineers: list[Engineer], cfg: Optional[ConstraintConfig]) -> str:
    """Return a human-readable reason explaining why *job* could not be assigned."""
    failures = []

    # Identify which HARD constraints block ALL engineers
    if cfg is None or cfg.skill_match.is_hard:
        if not any(_engineer_has_all_skills(e, job.required_skills) for e in engineers):
            failures.append(f"no engineer has required skill(s) [{', '.join(job.required_skills)}]")

    if cfg is None or cfg.workflow.is_hard:
        if not any(_engineer_matches_workflow(e, job.workflow_type) for e in engineers):
            failures.append(f"no engineer supports workflow [{job.workflow_type}]")

    if cfg is not None and cfg.worklocation.is_hard:
        loc = getattr(job, "location_name", None)
        if loc and not any(_engineer_matches_location(e, loc) for e in engineers):
            failures.append(f"no engineer covers location [{loc}]")

    if failures:
        return "HARD constraint(s) failed: " + "; ".join(failures)

    # Hard constraints pass for some engineer — capacity must be exhausted
    has_capable = any(_is_skill_workflow_eligible(e, job, cfg) for e in engineers)
    if not has_capable:
        return (
            f"No eligible engineer -- required skill(s): {', '.join(job.required_skills)}, "
            f"workflow: {job.workflow_type}"
        )
    return "No engineer with remaining capacity after all passes"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_MAX_RELAXED_PASSES = 3


def assign_jobs_to_engineers(
    engineers: list[Engineer],
    jobs: list[Job],
    travel_time_seconds: np.ndarray,
    travel_distance_meters: np.ndarray,
    location_index: dict,
    cfg: Optional[ConstraintConfig] = None,
    solve_date: str = "",
    break_duration_min: int = 15,
    solver_params: Optional[SolverParams] = None,
) -> tuple[dict[str, list[Job]], list[UnassignedJob]]:
    """Multi-pass assignment: hard checks first, then soft score + travel + preferred engineer."""
    if not jobs:
        return {e.engineer_id: [] for e in engineers}, []

    sp = solver_params or SolverParams()
    sd = solve_date or datetime.utcnow().strftime("%Y-%m-%d")

    # -- Phase 1: strict eligibility (all HARD+ENABLED constraints) ----------
    job_eligible: dict[str, list[str]] = {}
    for job in jobs:
        eligible = [e.engineer_id for e in engineers if is_hard_eligible(e, job, cfg)]
        if eligible:
            job_eligible[job.job_id] = eligible

    assignable = [j for j in jobs if j.job_id in job_eligible]
    engineer_jobs: dict[str, list[Job]] = {e.engineer_id: [] for e in engineers}
    assigned_ids: set[str] = set()

    # Passes 1-3: nearest-first per priority (round-robin)
    use_priority_passes = (cfg is None or cfg.priority.is_hard)
    if use_priority_passes:
        for priority in ("P1", "P2", "P3"):
            _nearest_first_pass(
                priority, engineers, engineer_jobs, assigned_ids, assignable,
                job_eligible, location_index, travel_distance_meters, travel_time_seconds,
                cfg, sd, break_duration_min, sp,
            )
    else:
        _nearest_first_pass(
            None, engineers, engineer_jobs, assigned_ids, assignable,
            job_eligible, location_index, travel_distance_meters, travel_time_seconds,
            cfg, sd, break_duration_min, sp,
        )

    # Pass 4: Hungarian batch for remaining (with spread validation)
    remaining = [j for j in assignable if j.job_id not in assigned_ids]
    if remaining:
        _hungarian_pass(
            remaining, engineers, engineer_jobs, assigned_ids, job_eligible,
            location_index, travel_distance_meters, travel_time_seconds, cfg,
            sd, break_duration_min, sp,
        )

    # Pass 5: greedy fill
    remaining = [j for j in assignable if j.job_id not in assigned_ids]
    if remaining:
        _greedy_pass(
            remaining, engineers, engineer_jobs, assigned_ids, job_eligible,
            location_index, travel_distance_meters, travel_time_seconds, cfg,
            sd, break_duration_min, sp,
        )

    # Pass 6: rebalance
    _rebalance_travel(
        engineers, engineer_jobs, job_eligible, location_index, travel_distance_meters,
        travel_time_seconds, cfg, sd, break_duration_min, sp,
    )

    # Pass 7: final retry with strict eligibility
    still_left = [j for j in assignable if j.job_id not in assigned_ids]
    if still_left:
        _greedy_pass(
            still_left, engineers, engineer_jobs, assigned_ids, job_eligible,
            location_index, travel_distance_meters, travel_time_seconds, cfg,
            sd, break_duration_min, sp,
        )

    # -- Phase 2: relaxed multi-pass ----------------------------------------
    for _ in range(_MAX_RELAXED_PASSES):
        remaining = [j for j in jobs if j.job_id not in assigned_ids]
        if not remaining:
            break
        before = len(assigned_ids)
        _relaxed_greedy_pass(
            remaining, engineers, engineer_jobs, assigned_ids,
            location_index, travel_distance_meters, travel_time_seconds, cfg,
            sd, break_duration_min, sp,
        )
        if len(assigned_ids) == before:
            break

    # -- Build unassigned list -----------------------------------------------
    unassigned: list[UnassignedJob] = []
    for j in jobs:
        if j.job_id in assigned_ids:
            continue
        reason = _unassigned_reason(j, engineers, cfg)
        unassigned.append(UnassignedJob(job_id=j.job_id, reason=reason))

    return engineer_jobs, unassigned
