"""V8 Fiche (V8FICHE): profiles, id candidates, anchors, value/period formatting, forecast judgement wording,
bundle shape (no database)."""

from __future__ import annotations

from typing import Any

from reva.v8.entity import common as C
from reva.v8.entity import forecast_part as F
from reva.v8.entity import profiles as P
from reva.v8.entity.bundle import empty_bundle, headline, sources_summary
from reva.v8.entity.resolve import Resolved


def test_profiles_load_and_aliases() -> None:
    assert P.profile_key("company") == "legal_entity"
    assert P.profile_key("species") == "taxon"
    assert P.profile_key("power_plant") == "facility"
    assert P.profile_key("conflict") == "armed_conflict"
    assert P.profile_key("chemical") == "compound"
    assert P.profile_key("pays") == "country"
    assert P.profile_key("nothing_like_this") is None
    for key, prof in P.profiles().items():
        assert prof.get("label_fr"), key
        assert 1 <= len(prof.get("headline") or []) <= 6, key


def test_kind_labels_are_french() -> None:
    assert P.kind_label_fr("compound") == "Composé chimique"
    assert P.kind_label_fr("chemical_class") == "Classe chimique"
    assert P.kind_label_fr("legal_entity") == "Entreprise"
    assert P.kind_label_fr(None).startswith("Entité")
    assert P.kind_label_fr("some_new_kind") == "Some new kind"


def test_id_candidates() -> None:
    assert "chembl:CHEMBL941" in P.id_candidates("drug", "CHEMBL941")
    assert "chebi:27732" in P.id_candidates("chemical", "CHEBI:27732")
    assert "ucdp:conflict:13243" in P.id_candidates("conflict", "13243")
    assert P.id_candidates("entity", "Q142")[0] == "Q142"


def test_anchor_lookup_by_alias() -> None:
    a = P.find_anchor("commodity", "brent")
    assert a is not None and a[1] == "crude_oil"
    assert P.find_anchor("commodity", "crude_oil") is not None
    assert P.find_anchor("drug", "brent") is None
    spec = a[2]
    # every anchor series names where it is read from; every headline points to a declared series / top
    keys = {s["key"] for s in spec["series"]} | {t["key"] for t in spec["top"]}
    for h in spec["headline"]:
        assert (h.get("anchor_series") or h.get("anchor_top")) in keys


def test_authority_and_value_links() -> None:
    assert P.authority_url("Q142") == "https://www.wikidata.org/wiki/Q142"
    assert P.authority_url("ucdp:conflict:13243") == "https://ucdp.uu.se/conflict/13243"
    assert P.authority_url("hgnc:76").endswith("HGNC:76")
    assert P.authority_url("nope:1") is None
    assert P.value_url("mondo:xref", "OMIM:125853") == "https://omim.org/entry/125853"
    assert P.value_url("unichem:drugbank_id", "DB00619") == "https://go.drugbank.com/drugs/DB00619"


def test_literal_values_keep_decimal_strings() -> None:
    q = C.literal_value("chebi:mass", {"type": "quantity", "amount": "+194.194", "unit": "Da"})
    assert q["value"] == "194.194" and q["unit"] == "Da"
    b = C.literal_value("x", {"type": "quantity", "amount": "99473", "lowerBound": "72533", "upperBound": "120000"})
    assert (b["lo"], b["hi"]) == ("72533", "120000")
    t = C.literal_value("x", {"type": "time", "time": "2001", "precision": 9})
    assert t["value"] is None and t["text"] == "2001"
    e = C.literal_value("chembl:atc_code", {"type": "external_id", "value": "L01EA01"})
    assert e["url"] and "L01EA01" in e["url"]
    assert C.literal_value("x", None)["value"] is None


def test_period_labels() -> None:
    assert C.period_label("2024-01-01", "2025-01-01", "calendar_year") == "2024"
    assert C.period_label("2024-03-01", "2024-04-01") == "2024-03"
    assert C.period_label("2024-04-01", "2024-07-01") == "2024-T2"
    assert C.period_label("2026-10-14", "2026-10-15") == "2026-10-14"
    assert C.period_label("2020-01-01", "2022-01-01") == "2020-01-01 → 2022-01-01"
    assert C.time_text({"time": "+2014-03-18T00:00:00Z", "precision": 11}) == "2014-03-18"
    assert C.time_text({"time": "-0043-03-15", "precision": 9}) == "-43"


def test_forecast_model_kinds_and_judgement() -> None:
    assert F.model_kind("forecast.reference_imf_weo", "IMF World Economic Outlook projection") == "official"
    assert F.model_kind("reva.ensemble", "REVA ensemble") == "ensemble"
    assert F.model_kind("forecast.registry_persistence", "random walk") == "baseline"
    assert F.model_kind("forecast.weo_error_fan", "BENCHMARK (FIX7): N(IMF WEO point ...") == "baseline"
    assert F.model_kind("forecast.energy_ardl", "candidate: direct k-step ARDL") == "candidate"
    # a pending question is never judged
    assert "pas encore jugée" in F.judged_fr("pending", [], "2027-05-01")
    rows: list[dict[str, Any]] = [
        {"kind": "official", "label": "FMI (WEO) — attribué", "score": {"abs_error": "0.5"}},
        {"kind": "candidate", "label": "x", "score": {"abs_error": "0.7"}},
    ]
    assert "ne bat pas FMI (WEO)" in F.judged_fr("resolved", rows, None)
    rows[1]["score"] = {"abs_error": "0.2"}
    assert "bat FMI (WEO)" in F.judged_fr("resolved", rows, None)
    assert "pas de comparaison" in F.judged_fr("resolved", rows[1:], None)


def test_forecast_probability_and_interval_records() -> None:
    assert F._prob({"prob_above": "0.97"}) == "0.97"
    assert F._prob({"ensemble": {"prob_above": "0.5"}}) == "0.5"
    assert F._prob({"point": "3"}) is None
    assert F._qband({"interval": ["1", "2"]}) == ("1", "2", None)
    assert F._qband({"quantiles": {"0.05": "1", "0.95": "3"}}) == ("1", "3", "90 %")


def _src(name: str, lic: str, restricted: bool = False) -> dict[str, Any]:
    return {"source": name, "label": name, "licence": {"licence": lic, "restricted": restricted, "non_commercial": False}}


def test_headline_missing_is_typed_never_zero() -> None:
    res = Resolved(kind="compound", id="chebi:1", via="qid", qid="chebi:1", kg_kind="compound")
    b = empty_bundle(res)
    b["facts"] = [
        {"property": "chebi:formula", "label_fr": "formule", "value": None, "value_text": "C8H10N4O2", "unit": None,
         "value_lo": None, "value_hi": None, "url": None, "period": None, "count": 1, "points": [], "long_text": False,
         "source": _src("chebi.release", "CC-BY-4.0"), "others": []},
    ]  # fmt: skip
    hs = headline(b, res, P.profile_for("compound"), None)
    by = {h["key"]: h for h in hs}
    assert by["chebi:formula"]["value_text"] == "C8H10N4O2"
    mass = by["chebi:mass"]
    assert mass["value"] is None and mass["missing"]["reason_fr"]  # absent: hatched with a reason, never 0
    assert by["made_of"]["missing"] is not None


def test_sources_summary_counts_and_licences() -> None:
    res = Resolved(kind="drug", id="chembl:CHEMBL941", via="qid", qid="chembl:CHEMBL941")
    b = empty_bundle(res)
    b["facts"] = [{"source": _src("chembl.api", "CC BY-SA 3.0", True)}, {"source": _src("chembl.api", "CC BY-SA 3.0", True)}]
    out = sources_summary(b)
    assert out == [
        {"source": "chembl.api", "label": "chembl.api", "n_values": 2, "licences": ["CC BY-SA 3.0"], "restricted": True,
         "non_commercial": False, "verification": None}
    ]  # fmt: skip


def test_route_models_accept_an_empty_bundle() -> None:
    from reva.api.routes.v8_entity import V8FicheBundle

    res = Resolved(kind="country", id="FRA", via="iso3", iso3="FRA")
    b = empty_bundle(res)
    b["meta"]["generated_at"] = "2026-10-18T00:00:00+00:00"
    m = V8FicheBundle.model_validate(b)
    assert m.canonical.id == "FRA" and m.headline == [] and m.space.country_iso3 == "FRA"
