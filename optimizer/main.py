# Route Optimization API - Entry point
#
# Sample curl command:
#   curl -X POST http://localhost:8000/optimize \
#     -H "Content-Type: application/json" \
#     -d '{
#       "engineers": [{
#         "engineer_id": "ENG001",
#         "base_location": {"lat": 12.9716, "lng": 77.5946},
#         "shift_start": "09:00",
#         "shift_end": "18:00",
#         "break_window": {"start": "13:00", "end": "13:30"},
#         "overtime_allowed": true,
#         "skills": ["fiber", "electrical"],
#         "skill_ratings": {"fiber": 5, "electrical": 4},
#         "max_jobs_per_shift": 8
#       }],
#       "jobs": [{
#         "job_id": "JOB2024001",
#         "location": {"lat": 12.9850, "lng": 77.6100},
#         "required_skills": ["fiber"],
#         "priority": "P1",
#         "sla_deadline": "2026-03-16T14:00:00",
#         "estimated_duration_min": 90,
#         "workflow_type": "Breakfix",
#         "preferred_engineer_id": "ENG001"
#       }]
#     }'

import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from optimizer.clustering import (
    assign_jobs_to_engineers,
    is_hard_eligible,
    _can_add_job,
    _can_add_job_relaxed,
    _is_skill_workflow_eligible,
    _total_score_for_assignment,
)
from optimizer.scoring import choose_engineer_with_preference
from optimizer.models import (
    ConstraintConfig,
    Engineer,
    EngineerRoute,
    Job,
    OptimizerOutput,
    SolverParams,
    SolveSummary,
    UnassignedJob,
)
from optimizer.router import (
    solve_engineer_route,
    solve_engineer_route_relaxed_p3,
    solve_engineer_route_slot_based,
)
from optimizer.travel_matrix import build_travel_matrix


def _setup_logging() -> Path:
    """Write all app/uvicorn logs to a rotating file under the project root."""
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "optimizer.log"

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10_000_000,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Avoid duplicate handlers on reload
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(file_handler)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in root.handlers):
        root.addHandler(console_handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "optimizer", "fastapi"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.propagate = True

    return log_file


_LOG_FILE = _setup_logging()
logger = logging.getLogger("optimizer")
logger.info("Optimizer logging initialized → %s", _LOG_FILE)

app = FastAPI(title="Route Optimization API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend (optional: mount only if frontend dir exists)
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIR), name="assets")

    @app.get("/")
    def serve_frontend():
        return FileResponse(_FRONTEND_DIR / "index.html")

    @app.get("/styles.css")
    def serve_css():
        return FileResponse(_FRONTEND_DIR / "styles.css")

    @app.get("/app.js")
    def serve_js():
        return FileResponse(_FRONTEND_DIR / "app.js")


def _parse_payload(payload: dict[str, Any]) -> tuple[list[Engineer], list[Job], int, ConstraintConfig, SolverParams]:
    """Returns (engineers, jobs, break_duration_min, cfg, solver_params).

    Slot sources (highest priority first):
      1. Per-engineer ``availableSlots`` / ``available_slots`` / ``slots``
         (handled inside ``Engineer.from_dict``)
      2. Global ``availableSlots`` / ``available_slots`` / ``slots``
         on the payload root — caps every engineer.

    ``cfg`` is parsed from the optional ``config`` key in the payload.
    If absent, ``ConstraintConfig.default()`` is used (original behaviour).
    """
    engineers = [Engineer.from_dict(e) for e in payload.get("engineers", [])]
    jobs = [Job.from_dict(j) for j in payload.get("jobs", [])]

    global_raw = (
        payload.get("availableSlots")
        or payload.get("available_slots")
        or payload.get("slots")
    )
    if global_raw is not None:
        cap = len(global_raw) if isinstance(global_raw, list) else int(global_raw)
        for eng in engineers:
            eng.slot_based = True
            if eng.available_slots is None:
                eng.available_slots = global_raw if isinstance(global_raw, list) else None
            eng.max_jobs_per_shift = min(eng.max_jobs_per_shift, cap)

    break_duration_min = int(payload.get("break_duration_min", 15))
    break_duration_min = max(0, min(60, break_duration_min))

    raw_cfg = payload.get("config")
    cfg = ConstraintConfig.from_dict(raw_cfg) if isinstance(raw_cfg, dict) else ConstraintConfig.default()

    solver_params = SolverParams.from_payload(payload)

    return engineers, jobs, break_duration_min, cfg, solver_params


def _build_location_list_and_index(
    engineers: list[Engineer], jobs: list[Job]
) -> tuple[list, dict]:
    """Build ordered list of locations and id -> index map. Order: engineers first, then jobs."""
    locations = []
    location_index = {}
    for e in engineers:
        locations.append(e.base_location)
        location_index[e.engineer_id] = len(locations) - 1
    for j in jobs:
        locations.append(j.location)
        location_index[j.job_id] = len(locations) - 1
    return locations, location_index


def _empty_route(engineer: Engineer, cluster_id: str) -> EngineerRoute:
    break_slot = (
        {} if engineer.slot_based
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


_PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}
MAX_SOLVE_PASSES = 3


def _solve_routes(
    engineers: list[Engineer],
    engineer_jobs: dict[str, list[Job]],
    travel_time_seconds,
    travel_distance_meters,
    location_index: dict,
    solve_date: str,
    break_duration_min: int,
    cfg: ConstraintConfig = None,
) -> tuple[dict[str, EngineerRoute], list[Job]]:
    """Solve per-engineer routes. Returns (routes_map, dropped_jobs)."""
    routes_map: dict[str, EngineerRoute] = {}
    dropped: list[Job] = []

    for e in engineers:
        ejobs = engineer_jobs.get(e.engineer_id, [])
        cluster_id = f"cluster_{engineers.index(e)}"

        if not ejobs:
            routes_map[e.engineer_id] = _empty_route(e, cluster_id)
            continue

        if e.slot_based:
            route = solve_engineer_route_slot_based(
                e, ejobs, cluster_id,
                travel_distance_meters, location_index, solve_date,
                cfg=cfg,
            )
            routes_map[e.engineer_id] = route
            continue

        route = solve_engineer_route(
            e, ejobs, cluster_id,
            travel_time_seconds, travel_distance_meters,
            location_index, solve_date,
            break_duration_min=break_duration_min,
            cfg=cfg,
        )
        if route is None:
            route = solve_engineer_route_relaxed_p3(
                e, ejobs, cluster_id,
                travel_time_seconds, travel_distance_meters,
                location_index, solve_date,
                break_duration_min=break_duration_min,
                cfg=cfg,
            )
        if route is None:
            dropped.extend(ejobs)
            engineer_jobs[e.engineer_id] = []
            routes_map[e.engineer_id] = _empty_route(e, cluster_id)
            continue

        routed_ids = {s.job_id for s in route.route}
        for j in ejobs:
            if j.job_id not in routed_ids:
                dropped.append(j)
        engineer_jobs[e.engineer_id] = [j for j in ejobs if j.job_id in routed_ids]
        routes_map[e.engineer_id] = route

    return routes_map, dropped


def run_optimizer(payload: dict[str, Any]) -> dict[str, Any]:
    """Run full optimization pipeline and return output dict."""
    start_ms = time.perf_counter()
    solve_date = datetime.utcnow().strftime("%Y-%m-%d")
    if payload.get("solve_date"):
        solve_date = payload["solve_date"][:10]

    logger.info(
        "Optimization started: solve_date=%s engineers=%d jobs=%d slots=%s",
        solve_date,
        len(payload.get("engineers") or []),
        len(payload.get("jobs") or []),
        len(payload.get("availableSlots") or payload.get("available_slots") or []),
    )
    engineers, jobs, break_duration_min, cfg, solver_params = _parse_payload(payload)
    if not engineers:
        raise ValueError("At least one engineer required")
    total_jobs = len(jobs)

    logger.info("Building travel matrix for %d locations", len(engineers) + len(jobs))
    locations, location_index = _build_location_list_and_index(engineers, jobs)
    travel_time_seconds, travel_distance_meters = build_travel_matrix(
        locations, solve_date
    )

    # ------------------------------------------------------------------
    # Phase 1: multi-pass clustering assignment
    # ------------------------------------------------------------------
    engineer_jobs, clustering_unassigned = assign_jobs_to_engineers(
        engineers,
        jobs,
        travel_time_seconds,
        travel_distance_meters,
        location_index,
        cfg,
        solve_date=solve_date,
        break_duration_min=break_duration_min,
        solver_params=solver_params,
    )
    logger.info(
        "Assignment complete: assigned=%d initially_unassigned=%d",
        sum(len(assigned) for assigned in engineer_jobs.values()),
        len(clustering_unassigned),
    )

    # ------------------------------------------------------------------
    # Phase 2: per-engineer route solving with retry
    # ------------------------------------------------------------------
    routes_map, route_dropped = _solve_routes(
        engineers, engineer_jobs,
        travel_time_seconds, travel_distance_meters,
        location_index, solve_date, break_duration_min,
        cfg=cfg,
    )
    logger.info("Initial route solving complete: dropped=%d", len(route_dropped))

    # Multi-pass: try to reassign route-dropped jobs to other engineers.
    # Later passes relax the location soft-constraint and widen spread
    # thresholds so that jobs are not left unassigned while slots exist.
    for _pass in range(MAX_SOLVE_PASSES):
        if not route_dropped:
            break

        logger.info("Route retry pass %d: pending_jobs=%d", _pass + 1, len(route_dropped))

        use_relaxed = _pass >= 1
        route_dropped.sort(
            key=lambda j: (
                _PRIORITY_ORDER.get(j.priority, 3),
                j.sla_deadline is None,
                j.sla_deadline or "",
            )
        )

        reassigned_eng_ids: set[str] = set()
        still_dropped: list[Job] = []

        for job in route_dropped:
            scored: list[tuple[str, float]] = []
            for e in engineers:
                if use_relaxed:
                    if not _is_skill_workflow_eligible(e, job, cfg):
                        continue
                    current = engineer_jobs.get(e.engineer_id, [])
                    if not _can_add_job_relaxed(
                        e, current, job, location_index, travel_distance_meters,
                        travel_time_seconds, cfg, solve_date, break_duration_min, solver_params,
                    ):
                        continue
                else:
                    if not is_hard_eligible(e, job, cfg):
                        continue
                    current = engineer_jobs.get(e.engineer_id, [])
                    if not _can_add_job(
                        e, current, job, location_index, travel_distance_meters,
                        travel_time_seconds, cfg, solve_date, break_duration_min, solver_params,
                    ):
                        continue
                c = _total_score_for_assignment(
                    e, job, current, location_index,
                    travel_distance_meters, travel_time_seconds, cfg, solver_params,
                )
                scored.append((e.engineer_id, c))

            chosen_id = choose_engineer_with_preference(
                scored,
                job.preferred_engineer_id,
                override_gap=solver_params.preferred_override_gap,
            )
            if chosen_id is not None:
                engineer_jobs[chosen_id].append(job)
                reassigned_eng_ids.add(chosen_id)
            else:
                still_dropped.append(job)

        if not reassigned_eng_ids:
            route_dropped = still_dropped
            break

        # Re-solve only the affected engineers (shorter time limit for retries)
        for eng_id in reassigned_eng_ids:
            e = next(eng for eng in engineers if eng.engineer_id == eng_id)
            ejobs = engineer_jobs[eng_id]
            cluster_id = f"cluster_{engineers.index(e)}"

            if e.slot_based:
                route = solve_engineer_route_slot_based(
                    e, ejobs, cluster_id,
                    travel_distance_meters, location_index, solve_date,
                    cfg=cfg,
                )
                routes_map[eng_id] = route
                continue

            route = solve_engineer_route(
                e, ejobs, cluster_id,
                travel_time_seconds, travel_distance_meters,
                location_index, solve_date,
                break_duration_min=break_duration_min,
                time_limit_seconds=10,
                cfg=cfg,
            )
            if route is None:
                route = solve_engineer_route_relaxed_p3(
                    e, ejobs, cluster_id,
                    travel_time_seconds, travel_distance_meters,
                    location_index, solve_date,
                    break_duration_min=break_duration_min,
                    time_limit_seconds=10,
                    cfg=cfg,
                )
            if route is None:
                still_dropped.extend(ejobs)
                engineer_jobs[eng_id] = []
                routes_map[eng_id] = _empty_route(e, cluster_id)
                continue

            routed_ids = {s.job_id for s in route.route}
            for j in ejobs:
                if j.job_id not in routed_ids:
                    still_dropped.append(j)
            engineer_jobs[eng_id] = [j for j in ejobs if j.job_id in routed_ids]
            routes_map[eng_id] = route

        route_dropped = still_dropped

    # ------------------------------------------------------------------
    # Build final output
    # ------------------------------------------------------------------
    unassigned: list[UnassignedJob] = list(clustering_unassigned)
    for j in route_dropped:
        unassigned.append(
            UnassignedJob(job_id=j.job_id, reason="Could not fit in any engineer's schedule")
        )

    engineer_routes = [routes_map[e.engineer_id] for e in engineers]

    assigned_count = sum(len(r.route) for r in engineer_routes)
    sla_met = sum(1 for r in engineer_routes for s in r.route if s.sla_met)
    sla_at_risk = sum(1 for r in engineer_routes for s in r.route if not s.sla_met)
    total_travel_km = sum(r.total_travel_km for r in engineer_routes)
    utilizations = [r.utilization_pct for r in engineer_routes if r.route]
    avg_util = sum(utilizations) / len(utilizations) if utilizations else 0.0

    solve_time_ms = (time.perf_counter() - start_ms) * 1000
    summary = SolveSummary(
        total_jobs=total_jobs,
        assigned_jobs=assigned_count,
        unassigned_jobs=len(unassigned),
        sla_met=sla_met,
        sla_at_risk=sla_at_risk,
        total_travel_km=total_travel_km,
        avg_utilization_pct=avg_util,
    )
    output = OptimizerOutput(
        solve_date=solve_date,
        solve_time_ms=solve_time_ms,
        summary=summary,
        engineer_routes=engineer_routes,
        unassigned=unassigned,
    )
    logger.info(
        "Optimization completed: assigned=%d unassigned=%d elapsed_ms=%.1f",
        assigned_count,
        len(unassigned),
        solve_time_ms,
    )
    return output.to_dict()


@app.post("/optimize")
def optimize(payload: dict[str, Any]):
    """POST /optimize - accepts input payload, returns optimization result JSON."""
    try:
        if payload is None or not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")
        logger.info(
            "POST /optimize received: engineers=%d jobs=%d solve_date=%s",
            len(payload.get("engineers") or []),
            len(payload.get("jobs") or []),
            payload.get("solve_date"),
        )
        result = run_optimizer(payload)
        summary = (result or {}).get("summary") or {}
        logger.info(
            "POST /optimize success: assigned=%s unassigned=%s",
            summary.get("assigned_jobs"),
            summary.get("unassigned_jobs"),
        )
        return result
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning("Optimization request rejected: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(
            "Optimization failed: engineers=%d jobs=%d",
            len((payload or {}).get("engineers") or []) if isinstance(payload, dict) else 0,
            len((payload or {}).get("jobs") or []) if isinstance(payload, dict) else 0,
        )
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


def main_cli():
    """CLI entry: read JSON from stdin or file, print result."""
    import json
    import sys
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r") as f:
            payload = json.load(f)
    else:
        payload = json.load(sys.stdin)
    result = run_optimizer(payload)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
