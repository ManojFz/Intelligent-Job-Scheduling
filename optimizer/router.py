"""OR-Tools CVRPTW route optimization with break and SLA constraints.

Key fixes over original:
- Distance is used in the arc-cost evaluator (time + km).
- P2/P3 time windows widened to shift end so jobs are not dropped prematurely.
- travel_km correctly computed before updating prev_node.
- Disjunction penalty raised to strongly prefer assignment over dropping.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from optimizer.models import ConstraintConfig

import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from optimizer.config import (
    BREAK_DURATION_MIN,
    BREAK_WINDOW_END_MIN,
    BREAK_WINDOW_START_MIN,
    DEFAULT_BREAK_DURATION_MIN,
    OVERTIME_END_MINUTES,
    P1_MAX_START_MINUTES,
    SHIFT_END_MINUTES,
    SHIFT_START_MINUTES,
    SOLVER_TIME_LIMIT_SECONDS,
)
from optimizer.models import ConstraintConfig, Engineer, EngineerRoute, Job, RouteStop


def _sla_deadline_to_minutes(sla_iso: Optional[str], solve_date: str) -> Optional[int]:
    """Convert an SLA deadline to minutes from shift start; None means no deadline."""
    if not sla_iso:
        return None
    try:
        dt = datetime.fromisoformat(sla_iso.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.strptime(sla_iso[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo:
        dt = dt.replace(tzinfo=None)
    base = datetime.strptime(solve_date + " 09:00:00", "%Y-%m-%d %H:%M:%S")
    delta = dt - base
    return max(0, int(delta.total_seconds() / 60))


def _job_late_window_minutes(
    job: Job,
    solve_date: str,
    shift_end_min: int,
    relaxed: bool = False,
    cfg: Optional["ConstraintConfig"] = None,
) -> int:
    """Late end of the time window in minutes from shift start.

    Priority constraint (Hard):
      P1: hard cap at P1_MAX_START_MINUTES (must start within 2 h).
      When priority is not Hard, P1 is treated like P2/P3.

    SLA constraint:
      Hard → use SLA deadline strictly for P2/P3 (no widening beyond deadline).
      Soft / Disabled → widen window to shift_end_min so the solver does not
                        drop jobs prematurely; SLA compliance tracked separately.
    """
    priority_hard = cfg is None or cfg.priority.is_hard
    sla_hard = cfg is None or cfg.sla.is_hard

    base_min = _sla_deadline_to_minutes(job.sla_deadline, solve_date)

    # An absent SLA does not constrain routing. A hard P1 constraint still
    # applies because it is a priority rule, independent of the SLA field.
    if base_min is None:
        return P1_MAX_START_MINUTES if job.priority == "P1" and priority_hard else shift_end_min

    if job.priority == "P1" and priority_hard:
        return min(base_min, P1_MAX_START_MINUTES)

    if sla_hard:
        # Strict: window ends at SLA deadline (capped at shift end for feasibility)
        upper = OVERTIME_END_MINUTES if relaxed else shift_end_min
        return min(base_min, upper) if base_min <= upper else upper

    # Soft / Disabled: widen so solver keeps the job in the route
    upper = OVERTIME_END_MINUTES if relaxed else shift_end_min
    return max(base_min, upper)


def _minutes_to_time_str(minutes_from_shift_start: int) -> str:
    total = 9 * 60 + minutes_from_shift_start
    h = total // 60
    m = total % 60
    return f"{h:02d}:{m:02d}"


def solve_engineer_route(
    engineer: Engineer,
    jobs: list[Job],
    cluster_id: str,
    travel_time_seconds: np.ndarray,
    travel_distance_meters: np.ndarray,
    location_index: dict,
    solve_date: str,
    relaxed_p3: bool = False,
    break_duration_min: int = DEFAULT_BREAK_DURATION_MIN,
    time_limit_seconds: int = SOLVER_TIME_LIMIT_SECONDS,
    cfg: Optional["ConstraintConfig"] = None,
) -> Optional[EngineerRoute]:
    """Solve CVRPTW for one engineer.

    Shift 09:00-18:00 (minutes from 09:00).  Mandatory break node 13:00-13:30.
    Gap of *break_duration_min* after each job before the next.
    Arc cost = travel_time (min) + travel_distance (km) to use distance.

    Overtime behaviour (controlled by ``cfg.overtime``):
      Hard    → respect ``engineer.overtime_allowed`` strictly (current default).
      Soft    → allow overtime even when overtime_allowed=False (try to fit more).
      Disabled → always allow overtime up to OVERTIME_END_MINUTES.
    """
    overtime_cfg = cfg.overtime if cfg is not None else None

    if overtime_cfg is None or overtime_cfg.is_hard:
        # Strict: honour engineer's declared overtime flag
        shift_end_min = OVERTIME_END_MINUTES if engineer.overtime_allowed else SHIFT_END_MINUTES
    else:
        # Soft or Disabled: allow overtime regardless of engineer setting
        shift_end_min = OVERTIME_END_MINUTES

    use_break = not engineer.slot_based

    if not jobs:
        break_slot = (
            {} if not use_break
            else {"start": engineer.break_window.start, "end": engineer.break_window.end}
        )
        return EngineerRoute(
            engineer_id=engineer.engineer_id,
            cluster_id=cluster_id,
            start_location=engineer.base_location,
            shift_start=engineer.shift_start,
            shift_end=engineer.shift_end,
            utilization_pct=0.0,
            total_travel_km=0.0,
            total_travel_min=0.0,
            overtime_min=0,
            route=[],
            break_slot=break_slot,
        )

    depot_idx = location_index[engineer.engineer_id]
    job_indices = [location_index[j.job_id] for j in jobs]
    n_jobs = len(jobs)
    n_nodes = n_jobs + (2 if use_break else 1)

    # ------------------------------------------------------------------
    # Build local time and distance matrices
    # ------------------------------------------------------------------
    time_matrix = np.zeros((n_nodes, n_nodes), dtype=np.int64)
    dist_matrix_km = np.zeros((n_nodes, n_nodes), dtype=np.float64)

    def _global_idx(node: int) -> int:
        if node == 0:
            return depot_idx
        if use_break and node == n_nodes - 1:
            return depot_idx
        return job_indices[node - 1]

    for i in range(n_nodes):
        for j in range(n_nodes):
            if i == j:
                continue
            idx_i = _global_idx(i)
            idx_j = _global_idx(j)
            time_matrix[i, j] = int(travel_time_seconds[idx_i, idx_j] / 60)
            dist_matrix_km[i, j] = travel_distance_meters[idx_i, idx_j] / 1000.0

    # ------------------------------------------------------------------
    # Time windows (minutes from 09:00)
    # ------------------------------------------------------------------
    time_windows = [(SHIFT_START_MINUTES, shift_end_min)]  # depot
    for j in jobs:
        late = _job_late_window_minutes(j, solve_date, shift_end_min, relaxed_p3, cfg)
        late = max(late, SHIFT_START_MINUTES)
        time_windows.append((SHIFT_START_MINUTES, late))
    if use_break:
        time_windows.append((BREAK_WINDOW_START_MIN, BREAK_WINDOW_END_MIN))

    service_times = (
        [0]
        + [j.estimated_duration_min + break_duration_min for j in jobs]
        + ([BREAK_DURATION_MIN] if use_break else [])
    )

    max_visits = min(len(jobs), engineer.max_jobs_per_shift) + (2 if use_break else 1)
    if max_visits < 1:
        return None

    # ------------------------------------------------------------------
    # OR-Tools model
    # ------------------------------------------------------------------
    def time_callback(from_index, to_index):
        fn = manager.IndexToNode(from_index)
        tn = manager.IndexToNode(to_index)
        return int(time_matrix[fn, tn] + service_times[fn])

    def cost_callback(from_index, to_index):
        fn = manager.IndexToNode(from_index)
        tn = manager.IndexToNode(to_index)
        return int(time_matrix[fn, tn] + round(dist_matrix_km[fn, tn]) + service_times[fn])

    try:
        manager = pywrapcp.RoutingIndexManager(n_nodes, 1, 0)
        routing = pywrapcp.RoutingModel(manager)

        time_cb_idx = routing.RegisterTransitCallback(time_callback)
        cost_cb_idx = routing.RegisterTransitCallback(cost_callback)

        routing.SetArcCostEvaluatorOfAllVehicles(cost_cb_idx)

        routing.AddDimension(time_cb_idx, 60 * 24, shift_end_min + 60, True, "Time")
        time_dimension = routing.GetDimensionOrDie("Time")

        for node in range(n_nodes):
            idx = manager.NodeToIndex(node)
            lb, ub = time_windows[node]
            if ub < lb:
                ub = lb
            time_dimension.CumulVar(idx).SetRange(lb, ub)

        for node in range(1, n_jobs + 1):
            routing.AddDisjunction([manager.NodeToIndex(node)], 1_000_000)

        def count_callback(from_index, to_index):
            return 1

        count_cb_idx = routing.RegisterTransitCallback(count_callback)
        routing.AddDimension(count_cb_idx, 0, max_visits, True, "Count")

        routing.CloseModel()

        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.seconds = time_limit_seconds

        solution = routing.SolveWithParameters(search_parameters)
    except Exception:
        return None

    if not solution:
        return None

    # ------------------------------------------------------------------
    # Extract route
    # ------------------------------------------------------------------
    route_sequence: list[int] = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        route_sequence.append(manager.IndexToNode(index))
        index = solution.Value(routing.NextVar(index))

    cumul_min = 0
    prev_node = 0
    stops: list[RouteStop] = []
    stop_number = 0

    for node in route_sequence:
        if node == 0:
            continue

        travel_min = int(time_matrix[prev_node, node])
        travel_km_from_prev = float(dist_matrix_km[prev_node, node])

        cumul_min += travel_min
        arrival_min = cumul_min
        start_min = arrival_min
        service = service_times[node]
        end_min = start_min + service
        cumul_min = end_min

        if 1 <= node <= n_jobs:
            stop_number += 1
            job = jobs[node - 1]
            sla_deadline_min = _sla_deadline_to_minutes(job.sla_deadline, solve_date)
            job_end_min = start_min + job.estimated_duration_min
            sla_slack = 0 if sla_deadline_min is None else sla_deadline_min - job_end_min
            sla_met = sla_deadline_min is None or job_end_min <= sla_deadline_min
            stops.append(RouteStop(
                stop=stop_number,
                job_id=job.job_id,
                location=job.location,
                travel_from_prev_min=float(travel_min),
                travel_from_prev_km=travel_km_from_prev,
                arrival_time=_minutes_to_time_str(arrival_min),
                start_time=_minutes_to_time_str(start_min),
                end_time=_minutes_to_time_str(job_end_min),
                sla_deadline=job.sla_deadline,
                sla_slack_min=float(sla_slack),
                sla_met=sla_met,
                priority=job.priority,
                workflow_type=job.workflow_type,
                estimated_duration_min=job.estimated_duration_min,
                location_name=job.location_name,
            ))

        prev_node = node

    total_travel_min = sum(s.travel_from_prev_min for s in stops)
    total_travel_km = sum(s.travel_from_prev_km for s in stops)
    visited_job_ids = {s.job_id for s in stops}
    total_work_min = sum(
        j.estimated_duration_min + break_duration_min
        for j in jobs if j.job_id in visited_job_ids
    ) + (BREAK_DURATION_MIN if use_break else 0)
    available_min = shift_end_min - SHIFT_START_MINUTES
    utilization_pct = (
        (total_work_min + total_travel_min) / available_min * 100.0
        if available_min else 0
    )
    overtime = max(0, int(cumul_min) - SHIFT_END_MINUTES)

    break_slot = (
        {} if not use_break
        else {"start": engineer.break_window.start, "end": engineer.break_window.end}
    )
    return EngineerRoute(
        engineer_id=engineer.engineer_id,
        cluster_id=cluster_id,
        start_location=engineer.base_location,
        shift_start=engineer.shift_start,
        shift_end=engineer.shift_end,
        utilization_pct=float(min(100.0, utilization_pct)),
        total_travel_km=float(total_travel_km),
        total_travel_min=float(total_travel_min),
        overtime_min=int(overtime),
        route=stops,
        break_slot=break_slot,
    )


def _slot_end_minutes(slot_label: str) -> Optional[int]:
    """Parse slot label like '08-10' → end hour as minutes from 09:00 shift start."""
    try:
        end_hour = int(slot_label.split("-")[1])
        return (end_hour - 9) * 60
    except (IndexError, ValueError):
        return None


_PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2}


def solve_engineer_route_slot_based(
    engineer: Engineer,
    jobs: list[Job],
    cluster_id: str,
    travel_distance_meters: np.ndarray,
    location_index: dict,
    solve_date: str,
    cfg: Optional["ConstraintConfig"] = None,
) -> EngineerRoute:
    """Slot-based routing: SLA-ordered assignment, one job per slot.

    Sort order for slot placement:
      1. SLA deadline  (earliest SLA → earliest slot)
      2. Priority      (P1 before P2 before P3)
      3. Distance from the current stop (nearest first)

    No OR-Tools, no continuous time, no break node, no duration calculation.
    """
    if not jobs:
        return EngineerRoute(
            engineer_id=engineer.engineer_id,
            cluster_id=cluster_id,
            start_location=engineer.base_location,
            shift_start=engineer.shift_start,
            shift_end=engineer.shift_end,
            utilization_pct=0.0,
            total_travel_km=0.0,
            total_travel_min=0.0,
            overtime_min=0,
            route=[],
            break_slot={},
        )

    slot_labels = engineer.available_slots or []
    depot_idx = location_index[engineer.engineer_id]

    def _sla_priority_key(job: Job) -> tuple:
        return (
            job.sla_deadline is None,
            job.sla_deadline or "",
            _PRIORITY_RANK.get(job.priority, 9),
        )

    def _distance_from(current_idx: int, job: Job) -> float:
        j_idx = location_index.get(job.job_id)
        return (
            travel_distance_meters[current_idx, j_idx] / 1000.0
            if j_idx is not None else 99999.0
        )

    # SLA and priority always take precedence. When multiple remaining jobs
    # have the same SLA and priority, choose the one nearest to the engineer's
    # current position: the base for the first job, then the previous job.
    remaining_jobs = list(jobs)
    sorted_jobs: list[Job] = []
    current_idx = depot_idx
    while remaining_jobs:
        best_key = min(_sla_priority_key(job) for job in remaining_jobs)
        candidates = [
            job for job in remaining_jobs
            if _sla_priority_key(job) == best_key
        ]
        next_job = min(
            candidates,
            key=lambda job: (_distance_from(current_idx, job), job.job_id),
        )
        sorted_jobs.append(next_job)
        remaining_jobs.remove(next_job)

        next_idx = location_index.get(next_job.job_id)
        if next_idx is not None:
            current_idx = next_idx

    stops: list[RouteStop] = []
    total_travel_km = 0.0
    prev_idx = depot_idx

    for i, job in enumerate(sorted_jobs):
        slot_num = i + 1
        slot_label = slot_labels[i] if i < len(slot_labels) else f"slot{slot_num}"

        j_idx = location_index.get(job.job_id)
        dist_km = (
            travel_distance_meters[prev_idx, j_idx] / 1000.0
            if j_idx is not None else 0.0
        )
        total_travel_km += dist_km

        slot_end = _slot_end_minutes(slot_label)
        if slot_end is not None:
            sla_min = _sla_deadline_to_minutes(job.sla_deadline, solve_date)
            sla_met = sla_min is None or slot_end <= sla_min
        else:
            sla_met = True

        stops.append(RouteStop(
            stop=slot_num,
            job_id=job.job_id,
            location=job.location,
            travel_from_prev_min=0.0,
            travel_from_prev_km=dist_km,
            arrival_time="",
            start_time="",
            end_time="",
            sla_deadline=job.sla_deadline,
            sla_slack_min=0.0,
            sla_met=sla_met,
            priority=job.priority,
            workflow_type=job.workflow_type,
            estimated_duration_min=0,
            location_name=job.location_name,
            slot_index=slot_num,
            slot_label=slot_label,
        ))

        if j_idx is not None:
            prev_idx = j_idx

    total_slots = len(slot_labels) if slot_labels else engineer.max_jobs_per_shift
    if total_slots <= 0:
        total_slots = 1
    utilization_pct = (len(stops) / float(total_slots)) * 100.0

    return EngineerRoute(
        engineer_id=engineer.engineer_id,
        cluster_id=cluster_id,
        start_location=engineer.base_location,
        shift_start=engineer.shift_start,
        shift_end=engineer.shift_end,
        utilization_pct=float(min(100.0, utilization_pct)),
        total_travel_km=float(total_travel_km),
        total_travel_min=0.0,
        overtime_min=0,
        route=stops,
        break_slot={},
    )


def solve_engineer_route_relaxed_p3(
    engineer: Engineer,
    jobs: list[Job],
    cluster_id: str,
    travel_time_seconds: np.ndarray,
    travel_distance_meters: np.ndarray,
    location_index: dict,
    solve_date: str,
    break_duration_min: int = DEFAULT_BREAK_DURATION_MIN,
    time_limit_seconds: int = SOLVER_TIME_LIMIT_SECONDS,
    cfg: Optional["ConstraintConfig"] = None,
) -> Optional[EngineerRoute]:
    """Retry with P2/P3 time windows extended to overtime."""
    return solve_engineer_route(
        engineer,
        jobs,
        cluster_id,
        travel_time_seconds,
        travel_distance_meters,
        location_index,
        solve_date,
        relaxed_p3=True,
        break_duration_min=break_duration_min,
        time_limit_seconds=time_limit_seconds,
        cfg=cfg,
    )
