"""Named, counter-based random streams.

Every stream is derived from (run_seed, model_id, entity_id, draw_index) through
``numpy.random.SeedSequence`` feeding a ``Philox`` counter-based bit generator.
A stream depends only on its name, never on worker scheduling, call order or how
many other streams were created. Baseline and intervention evaluations that use
the same key receive identical draws (common random numbers).
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

GENERATOR = "numpy.random.Philox"
DERIVATION = "SeedSequence(entropy=run_seed, spawn_key=(sha256(model_id\\x1fentity_id) words, draw_index))"
_MAX_SEED = 2**64 - 1


def _name_words(model_id: str, entity_id: str) -> tuple[int, ...]:
    digest = hashlib.sha256(f"{model_id}\x1f{entity_id}".encode()).digest()
    return tuple(int.from_bytes(digest[i : i + 4], "little") for i in range(0, 32, 4))


def seed_sequence(run_seed: int, model_id: str, entity_id: str, draw_index: int) -> np.random.SeedSequence:
    if not isinstance(run_seed, int) or not 0 <= run_seed <= _MAX_SEED:
        raise ValueError("run_seed must be an integer in [0, 2**64)")
    if not isinstance(draw_index, int) or draw_index < 0:
        raise ValueError("draw_index must be a non-negative integer")
    return np.random.SeedSequence(entropy=run_seed, spawn_key=(*_name_words(model_id, entity_id), draw_index))


def stream(run_seed: int, model_id: str, entity_id: str, draw_index: int) -> np.random.Generator:
    """An independent generator for one named draw."""
    return np.random.Generator(np.random.Philox(seed_sequence(run_seed, model_id, entity_id, draw_index)))


def stream_key(run_seed: int, model_id: str, entity_id: str, draw_index: int) -> str:
    """Stable identifier of a named stream (for manifests and checkpoints)."""
    words = seed_sequence(run_seed, model_id, entity_id, draw_index).generate_state(4, dtype=np.uint64)
    return "".join(f"{int(w):016x}" for w in words)


def seed_plan(run_seed: int, model_id: str, entity_ids: list[str], draws_per_entity: int) -> dict[str, Any]:
    """Seed-plan record pinned on every run, even when no random draws are used."""
    return {
        "run_seed": run_seed,
        "generator": GENERATOR,
        "numpy_version": np.__version__,
        "derivation": DERIVATION,
        "model_id": model_id,
        "streams": [
            {"entity_id": e, "draws": draws_per_entity,
             "first_stream_key": stream_key(run_seed, model_id, e, 0) if draws_per_entity else None}
            for e in sorted(entity_ids)
        ],
        "random_draws_used": draws_per_entity * len(entity_ids),
    }
