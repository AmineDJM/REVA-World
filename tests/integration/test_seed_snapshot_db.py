"""Owner seed then runtime-role confirmation in one process survive concurrent registry edits (FIX2)."""

from __future__ import annotations

from typing import Any

import pytest

import reva.cli.seed as seed_mod
from reva.kernel.errors import ConfigurationError

pytestmark = pytest.mark.db


def test_worker_confirms_the_owner_seed_after_registry_files_change(db: Any, monkeypatch) -> None:
    assert seed_mod.seed(db.owner, db.store)["status"] in ("seeded", "already_seeded")
    # another agent rewrites the crosswalk / registry files after the owner's seed (the e2e flake)
    monkeypatch.setattr(seed_mod, "_crosswalk_sha256", lambda: "f" * 64)
    monkeypatch.setattr(seed_mod, "load_registry", lambda: pytest.fail("registry re-read in the same process"))
    assert seed_mod.seed(db.worker, db.store)["status"] == "already_seeded"


def test_refusal_names_both_fingerprints(db: Any, monkeypatch) -> None:
    monkeypatch.setattr(seed_mod, "seed_fingerprint", lambda reg, contact_email=None: "c" * 64)
    with pytest.raises(ConfigurationError, match="migration owner") as e:
        seed_mod.seed(db.worker, db.store)
    assert e.value.details["fingerprint"] == "c" * 64 and e.value.details["latest_owner_fingerprint"]
    assert "latest owner seed" in str(e.value)
