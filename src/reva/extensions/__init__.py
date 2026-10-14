"""Typed extension interfaces for phases P9–P12 (architecture §16, §18.3, §25–§27).

Every module here declares real protocols and validated specification types
for a later phase. None of the corresponding capabilities is implemented in
this build: the entry points raise :class:`reva.kernel.errors.CapabilityGap`
via :func:`reva.capabilities.require`, never a placeholder output. The only
executable logic is pure validation and verification (spec validation, the
double-entry ledger verifier) that later implementations must satisfy.

Modules: ``io_sfc`` (P9 IO / stock-flow ledger), ``feedback`` (P9 coupling
solver contract), ``population`` (P10 households / privacy accounting),
``runner`` (P11 external isolated runner), ``decision`` (P12 decision search
and multi-actor reactions).

These modules import no database, HTTP or API code.
"""
