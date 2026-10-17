"""Prospective forecast registry (V5, agent PRED-REGISTRY).

Questions generated from atlas series (``registry/forecasts/question_specs.yaml``), baseline forecasts frozen and
sealed before resolution (sha256 of the canonical batch manifest, database-clock timestamp, pinned atlas table
versions), resolution from the declared source and proper scores, leaderboard and calibration, and a
pseudo-real-time back-test harness on final-vintage data (labelled as such).

Modules: :mod:`.periods` (period indices), :mod:`.specs` (question families), :mod:`.questions` (pure question
generation), :mod:`.forecasts` (pure forecasting of questions), :mod:`.scores` (pure scoring), :mod:`.data` (atlas
reads), :mod:`.service` (issue / resolve / back-test / job handler), :mod:`.read` (API reads).
"""
