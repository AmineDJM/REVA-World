"""Real adapters' conversions (KGCORE query dicts, ATLASCORE query dicts, LINKS records) and lazy wiring."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from reva.nlqa.adapters import (
    AtlasQueryAdapter,
    LinksPathAdapter,
    build_backends,
    link_kind,
    literal_text,
    statement_from_kg,
)

KG_EDGE = {
    "row_id": 7,
    "subject": {"qid": "Q155", "label": "Brazil"},
    "property": {
        "pid": "P37",
        "label_en": "official language",
        "label_fr": "langue officielle",
        "causal_assertion": False,
    },
    "object": {"qid": "Q5146", "label": "Portuguese"},
    "value": None,
    "start": None,
    "end": None,
    "point_in_time": None,
    "qualifiers": {},
    "provenance": {
        "source_key": "wikidata",
        "statement_id": "Q155$1234",
        "statement_key": "wikidata:Q155$1234",
        "source_revision": "2251234567",
        "raw_sha256": "e" * 64,
        "retrieved_at": "2026-10-15T10:00:00+00:00",
        "origin": "live",
        "rank": "preferred",
        "references": [{"P248": "Q1"}],
    },
    "direction": "out",
}


def test_statement_from_kg_query_dict() -> None:
    s = statement_from_kg(KG_EDGE, {"Q155": {"fr": "Brésil"}, "Q5146": {"fr": "portugais"}})
    assert s.statement_id == "Q155$1234" and s.source_revision == "2251234567" and s.rank == "preferred"
    assert s.subject_label("fr") == "Brésil" and s.object_label("fr") == "portugais"
    assert s.property_label("fr") == "langue officielle" and s.raw_sha256 == "e" * 64
    lit = statement_from_kg(
        {
            **KG_EDGE,
            "object": None,
            "property": {"pid": "P1082"},
            "value": {"type": "quantity", "amount": "+203080756", "unit": "1"},
            "point_in_time": {"time": "+2022-00-00T00:00:00Z", "precision": 9},
        }
    )
    assert lit.literal == "203080756" and lit.literal_unit is None and lit.point_in_time.display() == "2022"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ({"type": "time", "time": "+1822-09-07T00:00:00Z", "precision": 11}, "1822-09-07"),
        (
            {"type": "monolingualtext", "text": "República Federativa do Brasil", "language": "pt"},
            "República Federativa do Brasil",
        ),
        ({"type": "external_id", "value": "BRA"}, "BRA"),
        ({"type": "quantity", "amount": "8515767", "unit": "http://www.wikidata.org/entity/Q712226"}, "8515767"),
    ],
)
def test_literal_text(value: dict[str, Any], text: str) -> None:
    assert literal_text(value)[0] == text


class FakeAtlasQuery:
    class snapshot:
        sha256 = "f" * 64

    def __init__(self) -> None:
        self.cell = {
            "value": "58900",
            "value_text": None,
            "unit": "XOF",
            "source": "ilo",
            "dataset": "ilostat_mw",
            "indicator": "EAR_INEE_NOC_NB|cur=LCU",
            "series": "ilo.ilostat_mw.EAR_INEE_NOC_NB|cur=LCU",
            "provider_geo": "SEN",
            "entity_qid": None,
            "period_start": "2019-01-01",
            "period_end": "2020-01-01",
            "flags": {},
            "year": 2019,
            "provenance": {
                "raw_sha256": "d" * 64,
                "selector": {"file": "mw.csv", "row": 3},
                "table_artifact_sha256": "9" * 64,
                "dataset_key": "atlas.ilo.ilostat_mw",
                "dataset_id": "x",
            },
        }

    def series(self, catalog: Any, concept_id: str, iso3: str) -> dict[str, Any]:
        missing = {
            **self.cell,
            "value": None,
            "year": 2020,
            "period_start": "2020-01-01",
            "period_end": "2021-01-01",
            "flags": {"missing": "not_reported"},
        }
        sub = {**self.cell, "year": None, "period_start": "2019-01-01", "period_end": "2019-07-01"}
        return {"per_source": [{"series": self.cell["series"], "cells": [self.cell, missing, sub]}]}

    def best_cells(self, catalog: Any, concept_ids: Any, *, iso3: str | None = None, year: int | None = None):
        return [{"concept_id": "labour.minimum_wage_monthly", "iso3": "SEN", **self.cell}]


class FakeCatalog:
    class C:
        id = "labour.minimum_wage_monthly"
        labels: ClassVar[dict[str, str]] = {"fr": "Salaire minimum", "en": "Minimum wage"}
        unit = "LCU"
        domain = "labour"
        definition = "d"

        class S:
            key = "ilo.ilostat_mw.EAR_INEE_NOC_NB|cur=LCU"

        source_order = (S,)

    concepts: ClassVar[dict[str, Any]] = {"labour.minimum_wage_monthly": C}


def test_atlas_adapter_keeps_provenance_and_drops_missing_and_subannual_cells() -> None:
    a = AtlasQueryAdapter(FakeAtlasQuery(), FakeCatalog())
    assert a.name == "reva.atlas snapshot ffffffffffff"
    r = a.values("labour.minimum_wage_monthly", ["SEN"], None)
    (c,) = r.cells
    assert c.value == "58900" and c.year == 2019 and c.raw_sha256 == "d" * 64
    assert c.selector == '{"file": "mw.csv", "row": 3}'
    assert c.provenance["table_artifact_sha256"] == "9" * 64 and c.provenance["atlas_snapshot_sha256"] == "f" * 64
    assert r.sources_consulted == ("ilo.ilostat_mw.EAR_INEE_NOC_NB|cur=LCU",)
    assert a.values("labour.minimum_wage_monthly", ["SEN"], 2020).cells == ()  # a missing-flag row is not a value
    assert len(a.values("labour.minimum_wage_monthly", None, 2019).cells) == 1
    assert [c.year for c in a.series("labour.minimum_wage_monthly", "SEN", 2010, 2030).cells] == [2019]
    assert a.concepts()[0].source_order == ("ilo.ilostat_mw.EAR_INEE_NOC_NB|cur=LCU",)


def test_links_path_adapter_converts_statement_and_derived_link_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace as NS

    import reva.kg.query as kq
    import reva.links.service as ls

    stmt = NS(
        statement_id="Q155$1",
        subject="Q155",
        pid="P37",
        object="Q5146",
        value=None,
        start=None,
        end=None,
        point=None,
        rank="normal",
        references=(),
        source_key="wikidata",
        source_revision="9",
        retrieved_at=None,
        raw_sha256="e" * 64,
        origin="live",
    )
    ev = {
        "kind": "kg_statement",
        "source": "wikidata",
        "statement_id": "Q155$2",
        "subject": "Q155",
        "pid": "P1365",
        "object": "Q90000003",
        "source_revision": "9",
    }
    link = NS(
        link_type="former_colony_of",
        subject="Q155",
        object="Q45",
        evidence=(ev,),
        temporal=NS(label=lambda: "1500–1822"),
    )
    hops = [
        NS(edge=NS(stmt=stmt, link=None, forward=False, type_id="P37")),
        NS(edge=NS(stmt=None, link=link, forward=True, type_id="former_colony_of")),
    ]
    res = NS(paths=[NS(hops=hops)], labels={"Q155": {"fr": "Brésil"}, "Q45": {"fr": "Portugal"}})
    seen = {}

    def fake_path_between(conn, a, b, **q):  # type: ignore[no-untyped-def]
        seen.update(q)
        return res

    monkeypatch.setattr(ls, "path_between", fake_path_between)
    monkeypatch.setattr(kq, "property_info", lambda conn, pids: {"P37": {"label_fr": "langue officielle"}})

    class KG:
        @contextmanager
        def _conn(self):  # type: ignore[no-untyped-def]
            yield None

    (path,) = LinksPathAdapter(KG()).paths("Q5146", "Q45", max_hops=4, as_of="1950", limit=3)  # type: ignore[arg-type]
    assert seen == {"max_hops": 4, "as_of": 1950, "k": 3} and path.engine == "reva.links.paths"
    h1, h2 = path.hops
    assert (
        h1.statement.property == "P37" and not h1.forward and h1.statement.property_label("fr") == "langue officielle"
    )
    assert h2.link.link_type == "former_colony_of" and h2.link.period == "1500–1822"
    assert h2.link.statements[0].statement_id == "Q155$2" and h2.from_qid == "Q155" and h2.to_qid == "Q45"


def test_link_kind_groups() -> None:
    assert link_kind("land_border") == "border" and link_kind("former_colony_of") == "colonial"
    assert link_kind("shared_official_language") == "shared_language" and link_kind("x_new") == "x_new"


def test_build_backends_without_engine_reports_what_is_missing() -> None:
    b = build_backends(None)
    assert b.kg is None and b.atlas is None and b.search is None
    assert any("LINKS" in n for n in b.notes)
