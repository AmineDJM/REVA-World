"""REVA estimation: event-study / local-projection engine on atlas panels with coded events as treatments
(agent PRED-EVENTSTUDY, V5). Reproduces published results before producing REVA-run effect estimates.

Estimates are statistical associations under stated identifying assumptions (treatment onset timing,
parallel pre-trends, no anticipation, no confounding events in the window). REVA does not establish causation.
"""

from reva.estimation.core import (
    EstimationError,
    Grid,
    LPResult,
    LPSpec,
    event_study,
    hp_filter,
    local_projection,
    ols_fe,
    permutation_placebo,
)

__all__ = [
    "EstimationError",
    "Grid",
    "LPResult",
    "LPSpec",
    "event_study",
    "hp_filter",
    "local_projection",
    "ols_fe",
    "permutation_placebo",
]
