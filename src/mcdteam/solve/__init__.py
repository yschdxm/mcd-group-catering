"""求解层:需求建模、候选过滤与团餐方案搜索。"""

from .constraints import (
    CateringRequirements,
    parse_requirements,
    role_quota,
)
from .planner import (
    CandidatePool,
    CateringPlan,
    CateringPlanner,
    PlanLine,
    build_candidate_pool,
    cheapest_per_role,
    fit_plan_to_budget,
    preference_score,
    score_plan,
    verify_plan_with_api,
)

__all__ = [
    "CateringRequirements",
    "parse_requirements",
    "role_quota",
    "CandidatePool",
    "CateringPlan",
    "CateringPlanner",
    "PlanLine",
    "build_candidate_pool",
    "cheapest_per_role",
    "fit_plan_to_budget",
    "preference_score",
    "score_plan",
    "verify_plan_with_api",
]
