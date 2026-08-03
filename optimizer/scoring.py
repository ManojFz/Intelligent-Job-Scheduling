"""Soft scoring after all hard rules pass. Lower total score is better."""

from __future__ import annotations

from typing import Optional

from optimizer.models import ConstraintConfig, Engineer, Job

# Spec soft deltas (start at 0)
SCORE_CLOSER_TO_BASE = -5.0
SCORE_SAME_CLUSTER = -5.0
SCORE_LESS_TRAVEL = -5.0
SCORE_FAR_TRAVEL = 10.0
SCORE_OVERTIME_USED = 20.0
SCORE_CLUSTER_BREAK = 10.0
SCORE_LOWER_RATING = 5.0

# Optional tie-break: preferred engineer vs best score
DEFAULT_PREFERRED_OVERRIDE_GAP = 50.0

_SOFT_SKILL_KM = 30.0
_SOFT_WORKFLOW_KM = 20.0
_SOFT_LOCATION_KM = 15.0


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


def _avg_skill_rating_for_job(engineer: Engineer, job: Job) -> float:
    if not job.required_skills:
        return 5.0
    vals = [engineer.skill_ratings.get(s, 0) for s in job.required_skills]
    return sum(vals) / max(len(vals), 1)


def soft_config_violation_score(engineer: Engineer, job: Job, cfg: Optional[ConstraintConfig]) -> float:
    """Extra penalty when config marks skill/workflow/location/rating as Soft and violated."""
    if cfg is None:
        return 0.0

    acc = 0.0
    if cfg.skill_match.is_soft and not _engineer_has_all_skills(engineer, job.required_skills):
        acc += _SOFT_SKILL_KM
    if cfg.workflow.is_soft and not _engineer_matches_workflow(engineer, job.workflow_type):
        acc += _SOFT_WORKFLOW_KM
    if cfg.worklocation.is_soft and not _engineer_matches_location(engineer, getattr(job, "location_name", None)):
        acc += _SOFT_LOCATION_KM
    if cfg.rating.is_soft:
        for skill in job.required_skills:
            if engineer.skill_ratings.get(skill, 0) < 4:
                acc += float(SCORE_LOWER_RATING)
                break
    return acc


def compute_soft_score(
    engineer: Engineer,
    job: Job,
    travel_leg_km: float,
    travel_leg_min: float,
    dist_base_to_job_km: float,
    cluster_radius_km: float,
    cfg: Optional[ConstraintConfig],
    cluster_coherent: bool,
    overtime_used_min: float = 0.0,
) -> float:
    """Spec: start 0; cluster / travel / rating / overtime deltas; config soft violations."""
    score = 0.0

    if dist_base_to_job_km <= cluster_radius_km:
        score += SCORE_CLOSER_TO_BASE

    if cluster_coherent:
        score += SCORE_SAME_CLUSTER

    if travel_leg_km < 3.0:
        score += SCORE_LESS_TRAVEL

    if dist_base_to_job_km > 2.0 * max(cluster_radius_km, 1.0):
        score += SCORE_FAR_TRAVEL

    if overtime_used_min > 0:
        score += SCORE_OVERTIME_USED

    if cfg is not None and cfg.worklocation.is_soft:
        if not _engineer_matches_location(engineer, getattr(job, "location_name", None)):
            score += SCORE_CLUSTER_BREAK

    if cfg is None:
        avg_r = _avg_skill_rating_for_job(engineer, job)
        if avg_r < 4.0:
            score += SCORE_LOWER_RATING

    score += soft_config_violation_score(engineer, job, cfg)
    return score


def total_assignment_score(
    soft_score: float,
    travel_km: float,
    travel_min: float,
) -> float:
    """Minimize penalty + distance + travel time (minutes weighted)."""
    return soft_score + travel_km + travel_min * 0.1


def choose_engineer_with_preference(
    scored_candidates: list[tuple[str, float]],
    preferred_engineer_id: Optional[str],
    override_gap: float = DEFAULT_PREFERRED_OVERRIDE_GAP,
) -> Optional[str]:
    """Lowest score wins; prefer *preferred_engineer_id* unless another is *override_gap* better."""
    if not scored_candidates:
        return None
    best_id, best_s = min(scored_candidates, key=lambda x: x[1])
    if not preferred_engineer_id:
        return best_id

    pref_tuple = next((x for x in scored_candidates if x[0] == preferred_engineer_id), None)
    if pref_tuple is None:
        return best_id

    pref_s = pref_tuple[1]
    if pref_s - best_s >= override_gap:
        return best_id
    return preferred_engineer_id
