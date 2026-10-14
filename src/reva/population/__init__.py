"""Population recipes and weighted-representative materialisation (age × recorded sex).

Only the age × recorded-sex joint distribution of Eurostat ``demo_pjan``-like
tables is supported (single years and/or disjoint age bands; see ``recipe``). Records are weighted representatives of source cells, never real
individuals, and carry no other attributes.
"""

from reva.population.calibration import CalibrationResult, rake
from reva.population.diagnostics import diagnose
from reva.population.materialise import (
    MAX_RECORDS,
    Allocation,
    CellAllocation,
    ResolutionError,
    allocate,
    band_for_leaf,
    canonical_summary,
    exact_total_weight,
    exact_weight_sums,
    materialise,
    standard_age_bands,
    suggest_coarsening,
    summary_logical_hash,
    table_logical_hash,
    table_metadata,
    to_parquet_bytes,
)
from reva.population.recipe import (
    FORBIDDEN_ATTRIBUTES,
    SUPPORTED_ATTRIBUTES,
    ConsistencyCheck,
    ExcludedCell,
    InconsistentSourceTotals,
    LeafCell,
    MissingCell,
    PopulationRecipe,
    SourceCell,
    UnsupportedAttribute,
    build_recipe,
    check_attributes,
    finest_disjoint_cover,
    parse_age_code,
)

__all__ = [
    "FORBIDDEN_ATTRIBUTES",
    "MAX_RECORDS",
    "SUPPORTED_ATTRIBUTES",
    "Allocation",
    "CalibrationResult",
    "CellAllocation",
    "ConsistencyCheck",
    "ExcludedCell",
    "InconsistentSourceTotals",
    "LeafCell",
    "MissingCell",
    "PopulationRecipe",
    "ResolutionError",
    "SourceCell",
    "UnsupportedAttribute",
    "allocate",
    "band_for_leaf",
    "build_recipe",
    "canonical_summary",
    "check_attributes",
    "diagnose",
    "exact_total_weight",
    "exact_weight_sums",
    "finest_disjoint_cover",
    "materialise",
    "parse_age_code",
    "rake",
    "standard_age_bands",
    "suggest_coarsening",
    "summary_logical_hash",
    "table_logical_hash",
    "table_metadata",
    "to_parquet_bytes",
]
