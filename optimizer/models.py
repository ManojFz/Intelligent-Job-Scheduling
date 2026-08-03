"""Data models for the route optimization system."""

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Constraint configuration
# ---------------------------------------------------------------------------


@dataclass
class ConstraintRule:
    """A single constraint with status and enforcement type."""
    status: str = "Enabled"   # "Enabled" | "Disabled"
    type: str = "Hard"        # "Hard" | "Soft"

    @property
    def enabled(self) -> bool:
        return self.status.lower() == "enabled"

    @property
    def is_hard(self) -> bool:
        return self.enabled and self.type.lower() == "hard"

    @property
    def is_soft(self) -> bool:
        return self.enabled and self.type.lower() == "soft"

    @classmethod
    def from_dict(cls, d: dict) -> "ConstraintRule":
        return cls(status=d.get("status", "Enabled"), type=d.get("type", "Hard"))


def _rule(d: dict, key: str, default_status: str = "Enabled", default_type: str = "Hard") -> ConstraintRule:
    return ConstraintRule.from_dict(d[key]) if key in d else ConstraintRule(default_status, default_type)


@dataclass
class ConstraintConfig:
    """Runtime constraint settings parsed from the payload ``config`` block.

    Defaults replicate the original hardcoded behaviour so that payloads
    without a ``config`` key continue to work unchanged.
    """
    # Assignment eligibility constraints
    skill_match:             ConstraintRule = field(default_factory=lambda: ConstraintRule("Enabled", "Hard"))
    workflow:                ConstraintRule = field(default_factory=lambda: ConstraintRule("Enabled", "Hard"))
    worklocation:            ConstraintRule = field(default_factory=lambda: ConstraintRule("Enabled", "Soft"))
    # Capacity / scheduling constraints
    max_ticket_per_day:      ConstraintRule = field(default_factory=lambda: ConstraintRule("Enabled", "Hard"))
    overtime:                ConstraintRule = field(default_factory=lambda: ConstraintRule("Enabled", "Soft"))
    sla:                     ConstraintRule = field(default_factory=lambda: ConstraintRule("Enabled", "Hard"))
    priority:                ConstraintRule = field(default_factory=lambda: ConstraintRule("Enabled", "Hard"))
    # Soft cost-shaping constraints
    rating:                  ConstraintRule = field(default_factory=lambda: ConstraintRule("Enabled", "Soft"))
    engineer_base_location:  ConstraintRule = field(default_factory=lambda: ConstraintRule("Enabled", "Soft"))
    engineer_end_location:   ConstraintRule = field(default_factory=lambda: ConstraintRule("Disabled", "Soft"))

    @classmethod
    def from_dict(cls, d: dict) -> "ConstraintConfig":
        return cls(
            skill_match=_rule(d, "skill_match"),
            workflow=_rule(d, "workflow"),
            worklocation=_rule(d, "worklocation", "Enabled", "Soft"),
            max_ticket_per_day=_rule(d, "max_ticket_per_day"),
            overtime=_rule(d, "overtime", "Enabled", "Soft"),
            sla=_rule(d, "sla"),
            priority=_rule(d, "priority"),
            rating=_rule(d, "rating", "Enabled", "Soft"),
            engineer_base_location=_rule(d, "engineer_base_location", "Enabled", "Soft"),
            engineer_end_location=_rule(d, "engineer_end_location", "Disabled", "Soft"),
        )

    @classmethod
    def default(cls) -> "ConstraintConfig":
        return cls()


@dataclass
class SolverParams:
    """Tunable parameters from payload root (optional)."""

    travel_speed_kmph: float = 30.0
    cluster_radius_km: float = 15.0
    preferred_override_gap: float = 50.0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SolverParams":
        return cls(
            travel_speed_kmph=float(payload.get("travel_speed_kmph", 30)),
            cluster_radius_km=float(payload.get("cluster_radius_km", 15)),
            preferred_override_gap=float(payload.get("preferred_override_gap", 50)),
        )


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass
class Location:
    lat: float
    lng: float

    def to_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lng)

    def to_api_string(self) -> str:
        return f"{self.lat},{self.lng}"


@dataclass
class BreakWindow:
    start: str  # "13:00"
    end: str    # "13:30"


@dataclass
class Engineer:
    engineer_id: str
    base_location: Location
    shift_start: str
    shift_end: str
    break_window: BreakWindow
    overtime_allowed: bool
    skills: list[str]
    skill_ratings: dict[str, int]
    max_jobs_per_shift: int
    workflows: list[str]  # e.g. ["Breakfix", "Installation"]; empty = all
    locations: list[str]   # area names e.g. ["Hebbal", "ECity", "HSR"]; empty = all
    available_slots: list[str] = None  # e.g. ["08-10", "11-13", "14-16"]
    slot_based: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Engineer":
        bl = d.get("base_location") or {}
        if not isinstance(bl, dict):
            bl = {}
        bw = d.get("break_window") or {"start": "13:00", "end": "13:30"}
        if not isinstance(bw, dict):
            bw = {"start": "13:00", "end": "13:30"}
        max_from_shift = int(d.get("max_jobs_per_shift", 8) or 8)

        raw_slots = d.get("availableSlots") or d.get("available_slots") or d.get("slots")
        if isinstance(raw_slots, list):
            slot_count = len(raw_slots)
            available_slots = raw_slots
        elif raw_slots is not None:
            slot_count = int(raw_slots)
            available_slots = None
        else:
            slot_count = None
            available_slots = None

        effective_max = min(slot_count, max_from_shift) if slot_count is not None else max_from_shift

        return cls(
            engineer_id=d["engineer_id"],
            base_location=Location(
                lat=float(bl.get("lat") or 0.0),
                lng=float(bl.get("lng") or 0.0),
            ),
            shift_start=d.get("shift_start", "09:00"),
            shift_end=d.get("shift_end", "18:00"),
            break_window=BreakWindow(
                start=(bw.get("start") or "13:00"),
                end=(bw.get("end") or "13:30"),
            ),
            overtime_allowed=d.get("overtime_allowed", False),
            skills=d.get("skills", []) or [],
            skill_ratings=d.get("skill_ratings", {}) or {},
            max_jobs_per_shift=effective_max,
            workflows=d.get("workflows", []) or [],
            locations=d.get("locations", []) or d.get("areas", []) or [],
            available_slots=available_slots,
            slot_based=raw_slots is not None,
        )


@dataclass
class Job:
    job_id: str
    location: Location
    required_skills: list[str]
    priority: str  # P1, P2, P3
    sla_deadline: Optional[str]  # ISO datetime; None means no SLA deadline
    estimated_duration_min: int
    workflow_type: str
    preferred_engineer_id: Optional[str] = None
    location_name: Optional[str] = None  # area e.g. "Hebbal", "ECity", "HSR"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        loc = d.get("location") or {}
        if not isinstance(loc, dict):
            loc = {}
        return cls(
            job_id=d["job_id"],
            location=Location(
                lat=float(loc.get("lat") or 0.0),
                lng=float(loc.get("lng") or 0.0),
            ),
            required_skills=d.get("required_skills") or [],
            priority=d.get("priority", "P2") or "P2",
            sla_deadline=d.get("sla_deadline") or None,
            estimated_duration_min=int(d.get("estimated_duration_min", 60) or 60),
            workflow_type=d.get("workflow_type", "Breakfix") or "Breakfix",
            preferred_engineer_id=d.get("preferred_engineer_id"),
            location_name=d.get("location_name") or d.get("area"),
        )


@dataclass
class RouteStop:
    stop: int
    job_id: str
    location: Location
    travel_from_prev_min: float
    travel_from_prev_km: float
    arrival_time: str
    start_time: str
    end_time: str
    sla_deadline: Optional[str]
    sla_slack_min: float
    sla_met: bool
    priority: str
    workflow_type: str
    estimated_duration_min: int = 0
    location_name: Optional[str] = None
    slot_index: Optional[int] = None
    slot_label: Optional[str] = None


@dataclass
class EngineerRoute:
    engineer_id: str
    cluster_id: str
    start_location: Location
    shift_start: str
    shift_end: str
    utilization_pct: float
    total_travel_km: float
    total_travel_min: float
    overtime_min: int
    route: list[RouteStop]
    break_slot: dict  # {"start": "13:00", "end": "13:30"}


@dataclass
class UnassignedJob:
    job_id: str
    reason: str


@dataclass
class SolveSummary:
    total_jobs: int
    assigned_jobs: int
    unassigned_jobs: int
    sla_met: int
    sla_at_risk: int
    total_travel_km: float
    avg_utilization_pct: float


@dataclass
class OptimizerOutput:
    solve_date: str
    solve_time_ms: float
    summary: SolveSummary
    engineer_routes: list[EngineerRoute]
    unassigned: list[UnassignedJob]

    @staticmethod
    def _stop_dict(s: "RouteStop") -> dict[str, Any]:
        if s.slot_index is not None:
            return {
                "slot": int(s.slot_index),
                "slot_label": s.slot_label or "",
                "job_id": s.job_id,
                "location": {"lat": float(s.location.lat), "lng": float(s.location.lng)},
                "travel_from_prev_km": float(round(s.travel_from_prev_km, 1)),
                "sla_deadline": s.sla_deadline,
                "sla_met": bool(s.sla_met),
                "priority": s.priority,
                "workflow_type": s.workflow_type,
                "location_name": s.location_name,
            }
        return {
            "stop": int(s.stop),
            "job_id": s.job_id,
            "location": {"lat": float(s.location.lat), "lng": float(s.location.lng)},
            "travel_from_prev_min": int(round(s.travel_from_prev_min)),
            "travel_from_prev_km": float(round(s.travel_from_prev_km, 1)),
            "arrival_time": s.arrival_time,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "sla_deadline": s.sla_deadline,
            "sla_slack_min": int(round(s.sla_slack_min)),
            "sla_met": bool(s.sla_met),
            "priority": s.priority,
            "workflow_type": s.workflow_type,
            "estimated_duration_min": int(s.estimated_duration_min),
            "location_name": s.location_name,
        }

    def _engineer_route_dict(self, r: "EngineerRoute") -> dict[str, Any]:
        d = {
            "engineer_id": r.engineer_id,
            "cluster_id": r.cluster_id,
            "start_location": {"lat": float(r.start_location.lat), "lng": float(r.start_location.lng)},
            "shift_start": r.shift_start,
            "shift_end": r.shift_end,
            "utilization_pct": float(round(r.utilization_pct, 1)),
            "total_travel_km": float(round(r.total_travel_km, 1)),
            "total_travel_min": int(round(r.total_travel_min)),
            "overtime_min": int(r.overtime_min),
            "route": [self._stop_dict(s) for s in r.route],
        }
        if r.break_slot:
            d["break"] = r.break_slot
        return d

    def to_dict(self) -> dict[str, Any]:
        return {
            "solve_date": str(self.solve_date),
            "solve_time_ms": float(self.solve_time_ms),
            "summary": {
                "total_jobs": int(self.summary.total_jobs),
                "assigned_jobs": int(self.summary.assigned_jobs),
                "unassigned_jobs": int(self.summary.unassigned_jobs),
                "sla_met": int(self.summary.sla_met),
                "sla_at_risk": int(self.summary.sla_at_risk),
                "total_travel_km": float(round(self.summary.total_travel_km, 1)),
                "avg_utilization_pct": float(round(self.summary.avg_utilization_pct, 1)),
            },
            "engineer_routes": [
                self._engineer_route_dict(r)
                for r in self.engineer_routes
            ],
            "unassigned": [{"job_id": u.job_id, "reason": u.reason} for u in self.unassigned],
        }
