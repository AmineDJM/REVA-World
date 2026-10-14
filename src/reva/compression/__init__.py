"""Exactly conserving compression (coarsen/refine) of weighted population tables.

Compression only moves between resolutions the source supports (age bands ↔
single years, by recorded sex). Every operation returns a certificate stating
its scope, exact conservation checks, measured loss and validity conditions.
"""

from reva.compression.certificates import CompressionCertificate, ConservationCheck
from reva.compression.refine import UnsupportedRefinement, refine
from reva.compression.stratified import AgeBand, CompressionResult, coarsen, validate_bands

__all__ = [
    "AgeBand",
    "CompressionCertificate",
    "CompressionResult",
    "ConservationCheck",
    "UnsupportedRefinement",
    "coarsen",
    "refine",
    "validate_bands",
]
