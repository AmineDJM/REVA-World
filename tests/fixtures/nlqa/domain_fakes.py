"""In-memory fakes of the V6-domain ports (agent NLQA6) — TEST FIXTURES, origin ``test_fixture``, not real data.

Entities use the ``fixture:`` namespace; numbers, shares, forecasts and estimates are invented for the tests (the
wording, citations and honesty rules are what the server produces). Sources are named ``fixture.*``.
"""

from __future__ import annotations

from typing import Any

import causal_fakes as C
import nlqa_fakes as F

from reva.nlqa.catalog import build_catalog, fold
from reva.nlqa.causal_adapter import CausalServiceAdapter
from reva.nlqa.ports import (
    AtlasCell,
    AtlasResult,
    Backends,
    Concept,
    CrossResult,
    CrossRow,
    EffectTerm,
    Entity,
    EntityMatch,
    FamilyGroup,
    FamilyNeighbour,
    FamilyView,
    KgStatement,
)

SHA = "f" * 64
CC0 = {"licence": "CC0-1.0", "open_licence": True, "restricted": False}
CCBY = {"licence": "CC-BY-4.0", "open_licence": True, "restricted": False}
UNCLEAR = {"licence": "licence_unclear (fixture)", "open_licence": None, "restricted": False}


def ent(qid: str, fr: str, en: str, kind: str, iso3: str | None = None) -> Entity:
    return Entity(qid, {"fr": fr, "en": en}, {}, kind, 0, iso3=iso3)


E: dict[str, Entity] = {
    e.qid: e
    for e in (
        ent("fixture:methane", "méthane", "methane", "compound"),
        ent("fixture:carbon", "carbone", "carbon", "chemical_element"),
        ent("fixture:hydrogen", "hydrogène", "hydrogen", "chemical_element"),
        ent("fixture:petroleum", "pétrole", "petroleum", "material"),
        ent("fixture:alkanes", "alcanes", "alkanes", "compound_class"),
        ent("fixture:hydrocarbons", "hydrocarbures", "hydrocarbons", "compound_class"),
        ent("fixture:water", "eau", "water", "compound"),
        ent("fixture:heart_wd", "cœur", "heart", "wikidata_item"),
        ent("fixture:heart_anat", "heart (anatomy)", "heart (anatomy)", "anatomical_structure"),
        ent("fixture:left_ventricle", "ventricule gauche", "left ventricle", "anatomical_structure"),
        ent("fixture:right_atrium", "oreillette droite", "right atrium", "anatomical_structure"),
        ent("fixture:chr17", "chromosome 17", "chromosome 17", "chromosome"),
        ent("fixture:tp53", "TP53", "TP53", "gene"),
        ent("fixture:brca1", "BRCA1", "BRCA1", "gene"),
        ent("fixture:wolf", "loup", "wolf", "taxon"),
        ent("fixture:deer", "cerf", "deer", "taxon"),
        ent("fixture:rabbit", "lapin", "rabbit", "taxon"),
        ent("fixture:lion_a", "lion", "lion", "taxon"),
        ent("fixture:lion_b", "lion", "lion", "taxon"),
        ent("fixture:africa", "Afrique", "Africa", "continent"),
        ent("fixture:mammals", "mammifères", "mammals", "taxon"),
        ent("fixture:kov", "KOV Mine", "KOV Mine", "facility"),
        ent("fixture:glencore", "Glencore", "Glencore", "legal_entity"),
        ent("fixture:copper", "cuivre", "copper", "commodity"),
        ent("fixture:coal", "charbon", "coal", "energy_source"),
        ent("fixture:gas", "gaz naturel", "natural gas", "energy_source"),
        ent("fixture:india", "Inde", "India", "country", iso3="IND"),
        ent("fixture:china", "Chine", "China", "country", iso3="CHN"),
        ent("fixture:plant_1", "CENTRALE UN", "PLANT ONE", "facility"),
        ent("fixture:plant_2", "CENTRALE DEUX", "PLANT TWO", "facility"),
        ent("fixture:plant_3", "CENTRALE GAZ", "GAS PLANT", "facility"),
        ent("fixture:plant_4", "CENTRALE CHINE", "CHINA PLANT", "facility"),
        ent("fixture:mtb", "Mycobacterium tuberculosis", "Mycobacterium tuberculosis", "taxon"),
        ent("fixture:tuberculosis", "tuberculose", "tuberculosis", "disease"),
        ent("fixture:immunodeficiency", "immunodéficience", "immunodeficiency", "disease"),
        ent("fixture:cattle", "Bos taurus", "Bos taurus", "taxon"),
    )
}

#: (n, subject, property, object, out family, out label {fr,en}, in family, in label, in inverse, source, rights, share)
FR_EN = dict[str, str]


def lab(fr: str, en: str) -> FR_EN:
    return {"fr": fr, "en": en}


MADE = lab("est composé de", "is made of")
MADE_INV = lab("est un constituant de", "is a constituent of")
HAS_PART = lab("comprend", "has part")
PART_OF = lab("fait partie de", "is part of")
LOC = lab("est situé(e) dans", "is located in")
LOC_INV = lab("contient (lieu)", "contains (place)")
EATS = lab("mange", "eats")
EATEN = lab("est mangé par", "is eaten by")
IS_A = lab("est un(e)", "is a")
IS_A_INV = lab("a pour instance ou sous-classe", "has instance or subclass")
OWNED_BY = lab("est possédé ou contrôlé par", "is owned or controlled by")
OWNS = lab("possède ou contrôle", "owns or controls")
PRODUCES = lab("produit", "produces")
PRODUCED_BY = lab("est produit par", "is produced by")
CONTEXT = lab("a pour contexte", "has context")
CONTEXT_INV = lab("est le contexte de", "is the context of")
ASSOC = lab("est associé à", "is associated with")
INTERACTS = lab("interagit avec", "interacts with")
SAME = lab("même entité que", "same as")
PROPS = {
    "fixture:formula_element": lab("est composé de l'élément (formule)", "is made of the element (formula)"),
    "fixture:has_part": lab("comprend (anatomie)", "has part (anatomy)"),
    "fixture:part_of": lab("fait partie de", "part of"),
    "fixture:located_on": lab("situé sur le chromosome", "located on chromosome"),
    "fixture:eats": lab("mange (interaction enregistrée)", "eats (recorded interaction)"),
    "fixture:range": lab("aire de répartition", "range"),
    "fixture:subclass_of": lab("sous-classe de", "subclass of"),
    "fixture:owned_by": lab("propriétaire", "owner"),
    "fixture:produces": lab("produit (minerai)", "produces (commodity)"),
    "fixture:primary_fuel": lab("combustible principal", "primary fuel"),
    "fixture:country": lab("pays", "country"),
    "fixture:has_feature": lab("présente la caractéristique", "has feature"),
    "fixture:has_host": lab("a pour hôte", "has host"),
    "fixture:wikidata_item": lab("identifiant porté par l'élément", "identifier held by item"),
    "fixture:equity_owner_name": lab("actionnaire (texte libre)", "equity owner (free text)"),
    "fixture:formula": lab("formule (texte)", "formula (text)"),
}
EDGES: list[tuple[Any, ...]] = [
    (1, "fixture:methane", "fixture:formula_element", "fixture:carbon", "made_of", MADE, "made_of", MADE_INV, True,
     "fixture.chebi", CCBY, {"value": "0.7487", "fraction": "0.7487", "unit": None, "scale": "fraction", "qualifier": "mass_share"}),
    (2, "fixture:methane", "fixture:formula_element", "fixture:hydrogen", "made_of", MADE, "made_of", MADE_INV, True,
     "fixture.chebi", CCBY, {"value": "0.2513", "fraction": "0.2513", "unit": None, "scale": "fraction", "qualifier": "mass_share"}),
    (3, "fixture:petroleum", "fixture:formula_element", "fixture:alkanes", "made_of", MADE, "made_of", MADE_INV, True,
     "fixture.wikidata", CC0, {"value": "25", "fraction": "0.25", "unit": "%", "scale": "percent", "qualifier": "P1107"}),
    (4, "fixture:alkanes", "fixture:part_of", "fixture:hydrocarbons", "part_of", PART_OF, "has_part", HAS_PART, False,
     "fixture.chebi", CCBY, None),
    (5, "fixture:heart_anat", "fixture:has_part", "fixture:left_ventricle", "has_part", HAS_PART, "part_of", PART_OF, False,
     "fixture.uberon", CCBY, None),
    (6, "fixture:right_atrium", "fixture:part_of", "fixture:heart_anat", "part_of", PART_OF, "has_part", HAS_PART, False,
     "fixture.uberon", CCBY, None),
    (7, "fixture:heart_anat", "fixture:wikidata_item", "fixture:heart_wd", "same_as", SAME, "same_as", SAME, False,
     "fixture.wikidata", CC0, None),
    (8, "fixture:tp53", "fixture:located_on", "fixture:chr17", "located_in", LOC, "located_in", LOC_INV, True,
     "fixture.hgnc", CC0, None),
    (9, "fixture:brca1", "fixture:located_on", "fixture:chr17", "located_in", LOC, "located_in", LOC_INV, True,
     "fixture.hgnc", CC0, None),
    (10, "fixture:wolf", "fixture:eats", "fixture:deer", "eats", EATS, "eats", EATEN, True, "fixture.globi", UNCLEAR, None),
    (11, "fixture:wolf", "fixture:eats", "fixture:rabbit", "eats", EATS, "eats", EATEN, True, "fixture.globi", UNCLEAR, None),
    (12, "fixture:lion_b", "fixture:range", "fixture:africa", "located_in", LOC, "located_in", LOC_INV, True,
     "fixture.iucn", CCBY, None),
    (13, "fixture:lion_b", "fixture:subclass_of", "fixture:mammals", "is_a", IS_A, "is_a", IS_A_INV, True, "fixture.ncbi", CC0, None),
    (14, "fixture:wolf", "fixture:subclass_of", "fixture:mammals", "is_a", IS_A, "is_a", IS_A_INV, True, "fixture.ncbi", CC0, None),
    (15, "fixture:kov", "fixture:owned_by", "fixture:glencore", "owned_by", OWNED_BY, "owns", OWNS, False,
     "fixture.usgs", CC0, None),
    (16, "fixture:kov", "fixture:produces", "fixture:copper", "produces", PRODUCES, "produced_by", PRODUCED_BY, False,
     "fixture.usgs", CC0, None),
    (17, "fixture:plant_1", "fixture:primary_fuel", "fixture:coal", "context", CONTEXT, "context", CONTEXT_INV, True,
     "fixture.gppd", CCBY, None),
    (18, "fixture:plant_1", "fixture:country", "fixture:india", "located_in", LOC, "located_in", LOC_INV, True,
     "fixture.gppd", CCBY, None),
    (19, "fixture:plant_2", "fixture:primary_fuel", "fixture:coal", "context", CONTEXT, "context", CONTEXT_INV, True,
     "fixture.gppd", CCBY, None),
    (20, "fixture:plant_2", "fixture:country", "fixture:india", "located_in", LOC, "located_in", LOC_INV, True,
     "fixture.gppd", CCBY, None),
    (21, "fixture:plant_3", "fixture:primary_fuel", "fixture:gas", "context", CONTEXT, "context", CONTEXT_INV, True,
     "fixture.gppd", CCBY, None),
    (22, "fixture:plant_3", "fixture:country", "fixture:india", "located_in", LOC, "located_in", LOC_INV, True,
     "fixture.gppd", CCBY, None),
    (23, "fixture:plant_4", "fixture:primary_fuel", "fixture:coal", "context", CONTEXT, "context", CONTEXT_INV, True,
     "fixture.gppd", CCBY, None),
    (24, "fixture:plant_4", "fixture:country", "fixture:china", "located_in", LOC, "located_in", LOC_INV, True,
     "fixture.gppd", CCBY, None),
    (25, "fixture:mtb", "fixture:has_feature", "fixture:immunodeficiency", "associated_with", ASSOC, "associated_with",
     ASSOC, False, "fixture.mondo", CCBY, None),
    (26, "fixture:mtb", "fixture:has_host", "fixture:cattle", "interacts_with", INTERACTS, "interacts_with", INTERACTS,
     False, "fixture.globi", UNCLEAR, None),
]  # fmt: skip
LITERALS: dict[str, list[tuple[str, str, str, dict[str, Any]]]] = {
    "fixture:kov": [
        ("fixture:equity_owner_name", "Glencore plc", "fixture.usgs", {"share_percent": "75"}),
        ("fixture:equity_owner_name", "Gécamines", "fixture.usgs", {"share_percent": "25"}),
        ("fixture:equity_owner_name", "Glencore plc", "fixture.usgs", {"share_percent": "75"}),  # duplicate row
    ],
    "fixture:methane": [("fixture:formula", "CH4", "fixture.chebi", {})],
}
RIGHTS = {"fixture.usgs": CC0, "fixture.chebi": CCBY}


def statement(
    n: int, s: str, p: str, o: str | None, src: str, *, literal: str | None = None, quals=None
) -> KgStatement:
    return KgStatement(
        statement_id=f"fixture-st-{n:04d}",
        subject_qid=s,
        property=p,
        subject_labels=dict(E[s].labels),
        property_labels=PROPS[p],
        object_qid=o,
        object_labels=dict(E[o].labels) if o else {},
        literal=literal,
        qualifiers=quals or {},
        source_key=src,
        source_revision="fixture-1",
        retrieved_at="2026-10-17T00:00:00+00:00",
        raw_sha256=SHA,
        origin="test_fixture",
    )


class FakeFamilies:
    name = "fake relation families (test fixture)"

    def __init__(self, edges=EDGES) -> None:
        self.edges = list(edges)
        self.calls: list[tuple[str, ...]] = []

    def _neighbours(self, qid: str) -> list[FamilyNeighbour]:
        out = []
        for n, s, p, o, fo, lo, fi, li, inv, src, rights, share in self.edges:
            st = statement(n, s, p, o, src)
            if s == qid:
                out.append(FamilyNeighbour(fo, lo, False, E[o], st, share, rights, p.split(":")[1], fo == "same_as"))
            elif o == qid:
                out.append(FamilyNeighbour(fi, li, inv, E[s], st, share, rights, p.split(":")[1], fi == "same_as"))
        return out

    def families(self, qid: str, *, families, per_family: int) -> FamilyView:
        self.calls.append(("families", qid, ",".join(families)))
        ns = self._neighbours(qid)
        groups = []
        for fam in families:
            mine = [n for n in ns if n.family == fam]
            if mine:
                groups.append(FamilyGroup(fam, mine[0].reading, len(mine), tuple(mine[:per_family])))
        lits = tuple(
            (statement(900 + i, qid, p, None, src, literal=v, quals=q), RIGHTS.get(src))
            for i, (p, v, src, q) in enumerate(LITERALS.get(qid, []))
        )
        return FamilyView(
            qid=qid,
            labels=dict(E[qid].labels) if qid in E else {},
            groups=tuple(groups),
            literals=lits,
            identities=tuple(n for n in ns if n.identity),
            kind=E[qid].kind if qid in E else None,
        )

    def cross(self, a_qid, a_families, b_qid, b_families, *, limit) -> CrossResult:
        self.calls.append(("cross", a_qid, b_qid))
        cands = {s for _, s, *_ in self.edges} | {o for _, _, _, o, *_ in self.edges}
        rows = []
        for x in sorted(cands):
            ns = [n for n in self._neighbours(x) if not n.identity and not n.inverse]
            na = next((n for n in ns if n.neighbour.qid == a_qid and (not a_families or n.family in a_families)), None)
            nb = next((n for n in ns if n.neighbour.qid == b_qid and n.family in b_families), None)
            if na and nb and x not in (a_qid, b_qid):
                rows.append(CrossRow(E[x], na, nb))
        return CrossResult(tuple(rows[:limit]), len(rows))


class FakeKgDomains(C.FakeKgEvents):
    """The NLQA fake KG plus the domain fixture entities (exact label, then prefix ≥ 4 letters)."""

    def search_entities(self, text: str, *, kind, lang, limit) -> list[EntityMatch]:
        t = fold(text)
        exact = [
            EntityMatch(e, "label_exact", text) for e in E.values() if any(fold(v) == t for v in e.labels.values())
        ]
        if exact:
            return exact[:limit]
        words = [w for w in t.split() if len(w) > 1]
        if len(words) >= 2:
            multi = [
                EntityMatch(e, "label_words", text)
                for e in E.values()
                if all(any(w in fold(v).split() for v in e.labels.values()) for w in words)
            ]
            if multi:
                return multi[:limit]
        found = super().search_entities(text, kind=kind, lang=lang, limit=limit)
        if found:
            return found
        if len(t) >= 4:
            return [
                EntityMatch(e, "label_prefix", text)
                for e in E.values()
                if any(fold(v).startswith(t) for v in e.labels.values())
            ][:limit]
        return []

    def entity(self, qid: str) -> Entity | None:
        return E.get(qid) or super().entity(qid)


# --------------------------------------------------------------------------- causal (diseases caused by a pathogen)
def _claim(key: str, cause: str, effect: str) -> dict[str, Any]:
    c = C.claim(key, "Q91000001", "Q361", "has_cause", "T1", "B", dataset="doid", source_key="fixture.doid")
    for side, qid in (("cause", cause), ("effect", effect)):
        e = E[qid]
        c[side] = {"id": qid, "label": e.labels["en"], "labels": dict(e.labels), "kind": e.kind}
    return c


MTB_EFFECTS = [_claim("fixture:doid:0001", "fixture:mtb", "fixture:tuberculosis")]


def causal() -> CausalServiceAdapter:
    def explain(event_id: str, direction: str, limit: int) -> Any:
        rows = MTB_EFFECTS if direction == "effects" else []
        mine = [c for c in rows if c["cause" if direction == "effects" else "effect"]["id"] == event_id]
        return {"event": {"id": event_id}, "claims": mine[:limit]}

    def chains(a: str, b: str, max_hops: int, limit: int) -> Any:
        return {"from": a, "to": b, "found": False, "paths": []}

    return CausalServiceAdapter(explain, chains, name="fake causal claims (test fixture)")


# --------------------------------------------------------------------------- effects
TERMS = [
    EffectTerm("variable", "oil_price.global", {"fr": "Prix mondial du pétrole brut", "en": "Global crude oil price"}),
    EffectTerm("variable", "prices.inflation_rate", {"fr": "Taux d'inflation des prix à la consommation", "en": "Consumer price inflation rate"}),
    EffectTerm("atlas_concept", "imf.policy_rate", {"fr": "Taux directeur", "en": "Policy rate"}),
    EffectTerm("variable", "output.real_gdp_level", {"fr": "PIB réel (niveau)", "en": "Real GDP (level)"}),
]  # fmt: skip


def estimate(
    key: str, cause: str, effect: str, point: float, who: str, *, measure="response", unit="pp_per_percent"
) -> dict[str, Any]:
    t = {x.id: x for x in TERMS}
    c, e = t[cause], t[effect]
    val = str(point)
    fr = (
        f"Selon {who}, la réponse estimée de {e.labels['fr']} à un choc de 10 percent sur {c.labels['fr']} est de "
        f"{val} point(s) de pourcentage, peak à 1.0 year (méthode : svar) ; REVA n'établit pas cet effet."
    )
    en = (
        f"According to {who}, the estimated response of {e.labels['en']} to a shock of 10 percent in "
        f"{c.labels['en']} is {val} percentage point(s), peak at 1.0 year(s) (method: svar); REVA does not "
        f"establish this effect."
    )
    return {
        "estimate_key": key,
        "cause": {
            "kind": c.kind,
            "id": c.id,
            "label": c.labels["en"],
            "label_fr": c.labels["fr"],
            "definition": "fixture",
        },
        "effect": {
            "kind": e.kind,
            "id": e.id,
            "label": e.labels["en"],
            "label_fr": e.labels["fr"],
            "definition": "fixture",
        },
        "measure": measure,
        "unit": unit,
        "point": point,
        "as_printed": val,
        "sign": "positive" if point > 0 else "negative",
        "uncertainty": {"status": "not_reported"},
        "horizon": {"kind": "peak", "value": 1.0, "unit": "year"},
        "shock": {"size": 10, "unit": "percent"},
        "scope": {"countries": ["WLD"]},
        "method": "svar",
        "as_of": "2020-01-01",
        "wording": en,
        "wording_fr": fr,
        "origin": "test_fixture",
        "source": {
            "kind": "publication",
            "source_key": "fixture.effects_literature",
            "dataset": "fixture",
            "citation": {"short": who, "doi": "10.0000/fixture"},
            "locator": "Table 1 (fixture)",
            "quote": f"a 10 percent oil price increase raises inflation by {val} percentage points (fixture)",
            "raw_sha256": SHA,
            "rights": {"licence": "all rights reserved (fixture); short quotation with credit", "restricted": True},
            "retrieved_at": "2026-10-17T00:00:00+00:00",
        },
    }


ESTIMATES = [
    estimate("pub:fixture:oil_infl_a", "oil_price.global", "prices.inflation_rate", 0.2, "Fixture & Author (2019)"),
    estimate("pub:fixture:oil_infl_b", "oil_price.global", "prices.inflation_rate", 0.5, "Other Fixture (2021)"),
    estimate("pub:fixture:rate_gdp", "imf.policy_rate", "output.real_gdp_level", -0.3, "Rate Fixture (2018)"),
]


class FakeEffects:
    name = "fake effect estimates (test fixture)"

    def terms(self) -> list[EffectTerm]:
        return list(TERMS)

    def estimates(self, cause_ids, effect_ids, *, country, limit):
        return [
            e
            for e in ESTIMATES
            if (not cause_ids or e["cause"]["id"] in cause_ids) and (not effect_ids or e["effect"]["id"] in effect_ids)
        ][:limit]


# --------------------------------------------------------------------------- forecasts
MODELS = {
    "forecast.registry_persistence": ("1.0.0", "random walk / persistence with Gaussian h-step spread"),
    "forecast.registry_ar": ("1.0.0", "AR(p) on levels, OLS fitted as of issue, p by AIC"),
    "forecast.reference_imf_weo": ("1.0.0", "IMF World Economic Outlook projection, attributed to the IMF; point only"),
}
FAMILIES = [
    {"key": "weo.cpi_inflation", "label_fr": "Inflation (IPC, moyenne annuelle)", "label_en": "Inflation (CPI, annual average)",
     "countries": ["FRA", "DEU"], "status": "available", "status_reason": None},
    {"key": "weo.gdp_growth", "label_fr": "Croissance du PIB réel", "label_en": "Real GDP growth",
     "countries": ["FRA"], "status": "available", "status_reason": None},
    {"key": "eurostat.unemployment", "label_fr": "Taux de chômage (Eurostat, mensuel)", "label_en": "Unemployment rate (Eurostat, monthly)",
     "countries": ["FRA"], "status": "awaiting_table", "status_reason": "table atlas.eurostat.une_m not registered yet"},
]  # fmt: skip


def _q(qid: str, period: str, status: str) -> dict[str, Any]:
    return {
        "question_id": qid, "question_key": f"weo.cpi_inflation|FRA|{period}", "cycle": "2026-Q4",
        "family": "weo.cpi_inflation", "concept_id": "imf.inflation_cpi_avg", "label_fr": FAMILIES[0]["label_fr"],
        "label_en": FAMILIES[0]["label_en"], "geo": "FRA", "geo_label_fr": "France", "geo_label_en": "France",
        "reference_period": period, "type": "quantity", "unit": "% change (CPI, period average)",
        "text_fr": f"Inflation en France en {period} ?", "text_en": f"Inflation in France in {period}?",
        "expected_resolution": f"{int(period) + 1}-04-30", "void_after": f"{int(period) + 3}-01-01",
        "resolution_source": "atlas.imf.weo", "status": status, "outcome": "2.3" if status == "resolved" else None,
        "n_forecasts": 3, "batch_sha256": "1" * 64, "issued_at": "2026-10-17T10:00:00+00:00",
    }  # fmt: skip


QUESTIONS = {
    "fq_" + "a" * 24: _q("fq_" + "a" * 24, "2026", "pending"),
    "fq_" + "b" * 24: _q("fq_" + "b" * 24, "2024", "resolved"),
}


def _detail(qid: str) -> dict[str, Any]:
    q = QUESTIONS[qid]
    resolved = q["status"] == "resolved"
    fcs = [
        {"model": "forecast.registry_persistence", "status": "issued", "point": "1.9",
         "quantiles": {"0.05": "-0.4", "0.5": "1.9", "0.95": "4.2"},
         "score": {"model": "forecast.registry_persistence", "abs_error": 0.4, "crps": 0.31} if resolved else None},
        {"model": "forecast.registry_ar", "status": "issued", "point": "2.1",
         "quantiles": {"0.05": "0.2", "0.5": "2.1", "0.95": "4.0"},
         "score": {"model": "forecast.registry_ar", "abs_error": 0.2, "crps": 0.22} if resolved else None},
        {"model": "forecast.reference_imf_weo", "status": "abstained", "reason": "no WEO projection for this year"},
    ]  # fmt: skip
    return {
        "question": {**q, "resolution": {"expected_date": q["expected_resolution"], "source": "atlas.imf.weo"}},
        "forecasts": fcs,
        "outcome": {"status": "resolved", "value": "2.3", "source": "atlas.imf.weo (WEO 2025-04)"} if resolved else None,
        "status": q["status"],
        "seal": {"batch_sha256": "1" * 64, "issued_at": "2026-10-17T10:00:00+00:00", "verified": True,
                 "label": "BASELINE forecasts only (fixture)", "origin": "test_fixture"},
    }  # fmt: skip


class FakeForecasts:
    name = "fake forecast registry (test fixture)"

    def families(self):
        return list(FAMILIES)

    def questions(self, family, geo, *, limit):
        return [q for q in QUESTIONS.values() if q["family"] == family and (geo is None or q["geo"] == geo)][:limit]

    def question(self, question_id):
        return _detail(question_id) if question_id in QUESTIONS else None

    def models(self):
        return dict(MODELS)


# --------------------------------------------------------------------------- decision engine
class FakeDecisions:
    """Reference decisions and instruments from the real registry (``registry/predict``); ``run`` returns a
    fixture output in the engine's shape (scaled linearly by the magnitude, like the engine's linear scaling)."""

    name = "fake decision engine (test fixture)"

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    def references(self):
        from reva.predict.decision import reference_decisions

        return [d.model_dump(mode="json") for d in reference_decisions().values()]

    def instruments(self):
        from reva.predict.decision import instruments

        return [i.as_json() for i in instruments().values()]

    def run(self, decision, *, draws, seed):
        from reva.predict.engine import DISCLAIMERS_FR, LABEL_UNVALIDATED

        self.runs.append(dict(decision))
        m = float(decision["magnitude"]["value"])
        gdp = round(-0.0028 * m, 4)
        return {
            "engine": "reva.predict.engine/fixture",
            "label": LABEL_UNVALIDATED,
            "validation": {"status": "unvalidated", "label": LABEL_UNVALIDATED, "chain_signature": "c" * 64,
                           "reason": "no back-test and calibration record for this model chain"},
            "disclaimers_fr": list(DISCLAIMERS_FR),
            "decision": dict(decision),
            "settings": {"draws": draws, "seed": seed},
            "direct_changes": [],
            "variables": [
                {"id": "variable:output.real_gdp_level@FRA", "geo": "FRA", "unit": "percent", "kind": "estimate_chain",
                 "variable": {"kind": "variable", "id": "output.real_gdp_level", "label_fr": "PIB réel (niveau)",
                              "label_en": "Real GDP (level)"},
                 "path": ["h1", "h2"], "periods": [
                     {"period": "2027Q1", "p05": gdp * 2, "p25": gdp, "p50": gdp, "p75": gdp, "p95": gdp / 2, "mean": gdp,
                      "degenerate": False, "missing_reason": None}],
                 "undated": None},
            ],
            "hops": [
                {"id": "h1", "kind": "decision", "to": "atlas_concept:imf.policy_rate@FRA", "label_fr": "Changement direct décidé"},
                {"id": "h2", "kind": "estimate", "from": "atlas_concept:imf.policy_rate@FRA",
                 "to": "variable:output.real_gdp_level@FRA", "estimate_key": "pub:fixture:rate_gdp",
                 "source": {"source_key": "fixture.effects_literature", "citation": "Rate Fixture (2018)",
                            "rights": {"licence": "all rights reserved (fixture)", "restricted": True}}},
            ],
            "unquantified_links": [{"cause": "fixture", "effect": "fixture"}],
            "excluded_links": [], "alternative_paths": [], "gaps": [], "effect_layer": {"layer": "present"}, "chain": [],
        }  # fmt: skip


# --------------------------------------------------------------------------- atlas (catalog + cells)
EXTRA_CONCEPTS = [
    Concept("prices.daily.brent_spot", {"en": "Brent crude oil spot price (daily)", "fr": "Prix spot du pétrole brut Brent (quotidien)"},
            "Dollars per Barrel", "daily_markets", source_order=("fixture_eia.spot_d.RBRTE",), subdomain="energy_spot"),
    Concept("health.deaths_share.cardiovascular", {"en": "Share of deaths due to cardiovascular diseases",
            "fr": "Part des décès dus aux maladies cardiovasculaires"}, "% of total deaths", "health",
            source_order=("fixture_who.gho.CVD",), subdomain="causes_of_death"),
    Concept("health.deaths_share.injuries", {"en": "Share of deaths due to injuries", "fr": "Part des décès dus aux traumatismes"},
            "% of total deaths", "health", source_order=("fixture_who.gho.INJ",), subdomain="causes_of_death"),
]  # fmt: skip


def catalog():
    base = F.catalog()
    return build_catalog(concepts=[*base.concepts.values(), *EXTRA_CONCEPTS], catalog_dir=F.CATALOG_DIR)


def _cell(src, ds, ind, iso3, start, end, value, unit, concept, rights) -> AtlasCell:
    return AtlasCell(src, ds, ind, iso3, start, end, value, None, unit, concept, None, {}, SHA,
                     '{"file": "fixture.csv"}', None, {"rights": rights})  # fmt: skip


def _next_day(d: str) -> str:
    import datetime as dt

    return (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()


BRENT = [
    _cell("fixture_eia", "spot_d", "RBRTE", "", d, _next_day(d), v, "Dollars per Barrel", "prices.daily.brent_spot", CC0)
    for d, v in (("2020-01-02", "67.05"), ("2020-01-06", "70.25"), ("2020-04-21", "9.12"), ("2020-12-31", "51.22"),
                 ("2021-12-31", "77.24"))
]  # fmt: skip
DEATHS = [
    _cell("fixture_who", "gho", "CVD", "FRA", "2021-01-01", "2022-01-01", "23.26", "% of total deaths",
          "health.deaths_share.cardiovascular", CCBY),
    _cell("fixture_who", "gho", "INJ", "FRA", "2021-01-01", "2022-01-01", "6.42", "% of total deaths",
          "health.deaths_share.injuries", CCBY),
]  # fmt: skip


class FakeAtlasDomains(F.FakeAtlas):
    def __init__(self) -> None:
        super().__init__([*F.CELLS, *BRENT, *DEATHS])
        self.order.update({c.id: c.source_order for c in EXTRA_CONCEPTS})

    def period_cells(self, concept_id, from_year, to_year) -> AtlasResult:
        cells = [c for c in self._of(concept_id)]
        if from_year is None and to_year is None and cells:
            last = max(c.year for c in cells)
            from_year = to_year = last
        cells = [
            c for c in cells if (from_year is None or c.year >= from_year) and (to_year is None or c.year <= to_year)
        ]
        return AtlasResult(tuple(cells), tuple(self.order.get(concept_id, ())))


def backends(**over: Any) -> Backends:
    b = Backends(
        kg=FakeKgDomains(),
        atlas=FakeAtlasDomains(),
        links=F.FakeLinks(),
        causal=causal(),
        families=FakeFamilies(),
        effects=FakeEffects(),
        forecasts=FakeForecasts(),
        decisions=FakeDecisions(),
    )
    for k, v in over.items():
        setattr(b, k, v)
    return b
