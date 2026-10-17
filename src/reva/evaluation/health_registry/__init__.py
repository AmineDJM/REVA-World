"""Health forecast registry (V6, agent PRED-HEALTH): weekly surveillance and annual burden questions, quantile
forecasts scored by WIS, as-of back-tests against the US CDC forecast hubs' ensembles, sealed issues.

Pure parts: :mod:`.core` (specs, periods, series extraction, questions, forecasts, back-tests). Database parts:
:mod:`.service` (pins, seal, resolve, stored back-tests). CLI: ``python -m reva.evaluation.health_registry``.
"""
