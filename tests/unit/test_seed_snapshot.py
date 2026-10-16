"""Seed fingerprint is one registry snapshot per process (V3 e2e FIX2 seed flake).

The e2e stack seeds as the owner (``bootstrap``) and then confirms as ``reva_worker`` in the same process;
other agents edit ``registry/`` and the country crosswalk concurrently. Both calls must fingerprint the
same snapshot, read once, or the worker check refuses an owner seed that did happen."""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest

import reva.cli.seed as seed_mod


@pytest.fixture()
def fresh() -> Iterator[None]:
    seed_mod.reset_registry_snapshot()
    yield
    seed_mod.reset_registry_snapshot()


def test_fingerprint_is_computed_once_from_one_snapshot(fresh, monkeypatch) -> None:
    n = itertools.count()
    monkeypatch.setattr(seed_mod, "_crosswalk_sha256", lambda: f"{next(n):064x}")  # the file "changes" each read
    reg = seed_mod.registry_snapshot()
    assert seed_mod.registry_snapshot() is reg
    fp = seed_mod.seed_fingerprint(reg)
    assert seed_mod.seed_fingerprint(seed_mod.registry_snapshot()) == fp
    # the contact email changes no connector identity: same snapshot, same digest, no re-read
    assert seed_mod.seed_fingerprint(reg, "ops@example.org") == fp
    reads = next(n)
    assert reads == 1  # read once, with the registry
    # a registry loaded outside the snapshot is fingerprinted from the files as they are now
    assert seed_mod.seed_fingerprint(seed_mod.load_registry()) != fp
    # a new snapshot (new process / explicit reset) sees the new files
    seed_mod.reset_registry_snapshot()
    assert seed_mod.seed_fingerprint(seed_mod.registry_snapshot()) != fp


def test_registry_changes_after_the_owner_seed_do_not_break_the_runtime_confirmation(fresh, monkeypatch) -> None:
    """``seed`` loads its registry through the snapshot: a later edit is not seen by the same process."""
    reg = seed_mod.registry_snapshot()
    fp = seed_mod.seed_fingerprint(reg)
    monkeypatch.setattr(seed_mod, "load_registry", lambda: pytest.fail("seed re-read the registry files"))
    monkeypatch.setattr(seed_mod, "_crosswalk_sha256", lambda: "f" * 64)
    assert seed_mod.seed_fingerprint(seed_mod.registry_snapshot()) == fp
