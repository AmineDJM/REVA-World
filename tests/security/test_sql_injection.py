"""SQL injection: hostile strings through repositories, identity resolution and
concept lookups are bound parameters, i.e. inert data (§21 "prefer parameterised SQL").

Uses a private database because some checks write hostile strings.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.postgres import repositories as repo
from reva.belief.selection import eligible_latest
from reva.kernel.errors import ValidationProblem

pytestmark = pytest.mark.db

HOSTILE = [
    "x'); DROP TABLE evidence.observation;--",
    "FRA' OR '1'='1",
    "251' UNION SELECT id FROM world.entity --",
    "'; UPDATE meta.artifact SET privacy_class='sensitive_restricted'; --",
    "$$; DROP SCHEMA evidence CASCADE; $$",
    "\\'; TRUNCATE ops.audit; --",
    "%",
    "_",
    "FRA\x00",
    "Robert'); DELETE FROM world.alias WHERE ('1'='1",
]
TABLES = [
    "evidence.observation",
    "evidence.source",
    "world.entity",
    "world.alias",
    "meta.artifact",
    "ontology.concept_version",
    "ops.audit",
    "ops.job",
]


def _helper() -> Any:
    name = "reva_h2_isolated_db"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "fixtures" / "adversarial" / "isolated_db.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def sdb(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    with _helper().isolated_db(tmp_path_factory) as d:
        yield d


def snapshot(d: Any) -> dict[str, int]:
    with d.owner.connect() as c:
        out = {}
        for t in TABLES:
            assert c.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar_one() is not None, f"{t} dropped"
            out[t] = int(c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one())
        out["sensitive"] = int(
            c.execute(text("SELECT count(*) FROM meta.artifact WHERE privacy_class <> 'public_aggregate'")).scalar_one()
        )
        return out


def _nul_safe(s: str) -> bool:
    return "\x00" not in s


@pytest.mark.parametrize("s", HOSTILE)
def test_repository_lookups_treat_input_as_data(sdb: Any, s: str) -> None:
    before = snapshot(sdb)
    with sdb.worker.connect() as c:
        if _nul_safe(s):
            assert repo.resolve_alias(c, "worldbank", s) is None
            assert repo.resolve_alias(c, s, "FRA") is None
            assert repo.source_by_key(c, s) is None
            assert repo.connector_version_row(c, s, "1.0.0") is None
            assert repo.connector_version_row(c, "worldbank.wdi.indicators", s) is None
            with pytest.raises(KeyError):
                repo.concept_version_id(c, s)
            with pytest.raises(KeyError):
                repo.concept_version_id(c, "demography.population.wdi_midyear_total", s)
        else:
            # PostgreSQL text cannot hold NUL: rejected by the driver, never interpolated.
            with pytest.raises(Exception):
                repo.resolve_alias(c, "worldbank", s)
    assert snapshot(sdb) == before


@pytest.mark.parametrize("s", [h for h in HOSTILE if _nul_safe(h)])
def test_concept_filters_and_selection_treat_input_as_data(sdb: Any, s: str) -> None:
    before = snapshot(sdb)
    with sdb.api.connect() as c:
        assert eligible_latest(c, datetime.now(UTC), [s], None) == []
        assert eligible_latest(c, datetime.now(UTC), ["test_fixture", "live"], [s]) == []
    assert snapshot(sdb) == before


@pytest.mark.parametrize("s", [h for h in HOSTILE if _nul_safe(h)])
def test_identity_resolution_treats_input_as_data(sdb: Any, s: str) -> None:
    identities = pytest.importorskip("reva.world.identities")
    before = snapshot(sdb)
    with sdb.api.connect() as c:
        r = identities.resolve_alias(c, "worldbank", s)
        assert r.status == "unresolved" and r.entity_id is None
        assert identities.resolve_alias(c, s, "FRA").status == "unresolved"
        assert identities.key_scopes(c, s) == {}
        # A legitimate key still resolves (the hostile call did not corrupt anything).
        assert identities.resolve_alias(c, "worldbank", "FRA").status == "resolved"
    assert snapshot(sdb) == before


@pytest.mark.parametrize("s", [h for h in HOSTILE if _nul_safe(h)])
def test_entity_listing_filters_treat_input_as_data(sdb: Any, s: str) -> None:
    entities = pytest.importorskip("reva.world.entities")
    before = snapshot(sdb)
    with sdb.api.connect() as c:
        page = entities.list_entities(c, label_contains=s)
        # LIKE metacharacters are escaped: '%' or '_' match only literal characters.
        assert all(s in e.label for e in page.items)
        assert entities.list_entities(c, kind=s).items == ()
        assert entities.list_entities(c, namespace=s).items == ()
        assert entities.list_entities(c, alias_provider=s).items == ()
        with pytest.raises(ValidationProblem):
            entities.list_entities(c, cursor=s)
    assert snapshot(sdb) == before


def test_hostile_strings_written_through_narrow_paths_round_trip_verbatim(sdb: Any) -> None:
    before = snapshot(sdb)
    payload = {"note": HOSTILE[0], "nested": [HOSTILE[3], {"k": HOSTILE[4]}]}
    key = "h2-" + HOSTILE[1].replace(" ", "_")
    with sdb.api.begin() as c:
        jid = c.execute(
            text("SELECT ops.submit_job('replay', CAST(:p AS jsonb), :k, 0, :by)"),
            {"p": json.dumps(payload), "k": key, "by": HOSTILE[5]},
        ).scalar_one()
        repo.audit(c, HOSTILE[9], HOSTILE[0], {"target": HOSTILE[2]}, {"d": HOSTILE[4]})
    with sdb.owner.connect() as c:
        row = c.execute(
            text("SELECT payload, idempotency_key, created_by FROM ops.job WHERE id = :j"), {"j": jid}
        ).one()
        audit = c.execute(text("SELECT actor, action, target FROM ops.audit WHERE actor = :a"), {"a": HOSTILE[9]}).one()
    assert row[0] == payload and row[1] == key and row[2] == HOSTILE[5]
    assert audit[0] == HOSTILE[9] and audit[1] == HOSTILE[0] and audit[2] == {"target": HOSTILE[2]}
    after = snapshot(sdb)
    assert after["ops.job"] == before["ops.job"] + 1 and after["ops.audit"] == before["ops.audit"] + 1
    assert {k: v for k, v in after.items() if k not in ("ops.job", "ops.audit")} == {
        k: v for k, v in before.items() if k not in ("ops.job", "ops.audit")
    }


def test_hostile_job_type_is_rejected_not_executed(sdb: Any) -> None:
    before = snapshot(sdb)
    with pytest.raises(Exception, match="cannot be submitted"), sdb.api.begin() as c:
        c.execute(
            text("SELECT ops.submit_job(:t, '{}'::jsonb, 'h2-hostile-type-1', 0, 'api')"),
            {"t": "replay'; DROP TABLE ops.job; --"},
        )
    assert snapshot(sdb) == before
