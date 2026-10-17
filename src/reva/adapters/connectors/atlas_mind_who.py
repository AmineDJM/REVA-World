"""WHO mental health -> atlas table ``who.gho_mental`` (V4, agent V4MIND).

Source: WHO Global Health Observatory OData API (``ghoapi.azureedge.net``, CC BY 4.0 + WHO terms; budget key
``who.gho`` shared with A-HEALTHEDU and A-SOCIETY). Same request shape, parser and cell rules as A-HEALTHEDU's
``who.gho`` table (:mod:`reva.adapters.connectors.atlas_who`: country rows only, ``NumericValue`` verbatim, text
answers in ``value_text``, every dimension folded into the indicator); a separate table so each agent's curated
list stays independent. Indicator names are checked against the live GHO indicator list on every run (a code
that disappeared or was archived blocks registration).

Indicators (GHO codes and names verbatim from the indicator list, read 2026-10-17):

* WHO **Mental Health Atlas** (country questionnaires, ``MH_*``): mental health policy / plan / legislation (yes/no
  answers kept as text), year of the law and of the policy, government expenditure on mental health, the mental
  health workforce per 100,000 (psychiatrists, nurses, social workers, psychologists, occupational therapists,
  other non-specialised doctors, total), facilities, beds, admissions and outpatient visits per 100,000.
  ``MH_12`` (age-standardised suicide rate) is already in A-HEALTHEDU's ``who.gho`` and is not requested here.
* ``GDO_q35`` Estimated population-based prevalence of depression (one year, 2015, in the API).
* ``RSUD_1`` / ``RSUD_2`` point prevalence (%) of alcohol / drug use disorders, 15+ years (2004 in the API).

``SDG_SH_STA_SCIDEN`` (Number of suicide deaths) has no country row in the API (2026-10-17) and is not requested;
suicide rates are in A-HEALTHEDU's ``who.gho`` (``MH_12``, ``SDGSUICIDE``) and A-UN's ``un_sdg.sdg_headline``.

``flags.estimate_class``: Mental Health Atlas items are ``country_reported`` (Member State questionnaire answers
compiled by WHO); the prevalence estimates are WHO estimates (``who_comparable_estimate``; the API
does not state the method per row). Classification by V4MIND from the GHO indicator names, recorded per cell.

Prevalence, workforce and suicide figures describe populations, not individuals; they are associations at the
country level, never causes.
"""

from __future__ import annotations

import json
from typing import Any

from reva.adapters.connectors.atlas_society_common import canon, concept_hints, err, fetch_archive
from reva.adapters.connectors.base import module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.who_mental"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_mind_who/1"
DATASET = "gho_mental"
CATALOG_FILE = "mind.yaml"
FIXTURE_COUNTRIES = ("BRA", "FRA", "IND", "NGA", "JPN")
MHA = "WHO Mental Health Atlas (Member State questionnaire)"
CLASSIFIED_BY = "V4MIND catalog, from the GHO indicator names"

#: (code, unit as WHO states it in the indicator name, estimate class, producer)
_SPEC: tuple[tuple[str, str, str, str], ...] = (
    ("MH_1", "answer (text)", "country_reported", MHA),
    ("MH_2", "answer (text)", "country_reported", MHA),
    ("MH_3", "answer (text)", "country_reported", MHA),
    ("MH_25", "answer (text)", "country_reported", MHA),
    ("MH_26", "year", "country_reported", MHA),
    ("MH_27", "year", "country_reported", MHA),
    ("MH_4", "% of total government expenditures on health", "country_reported", MHA),
    ("MH_5", "% of total government expenditures on mental health", "country_reported", MHA),
    ("MH_6", "per 100,000", "country_reported", MHA),
    ("MH_7", "per 100,000", "country_reported", MHA),
    ("MH_8", "per 100,000", "country_reported", MHA),
    ("MH_9", "per 100,000", "country_reported", MHA),
    ("MH_21", "per 100,000", "country_reported", MHA),
    ("MH_22", "per 100,000", "country_reported", MHA),
    ("MH_23", "per 100,000", "country_reported", MHA),
    ("MH_10", "per 100,000", "country_reported", MHA),
    ("MH_11", "per 100,000", "country_reported", MHA),
    ("MH_13", "per 100,000", "country_reported", MHA),
    ("MH_14", "per 100,000", "country_reported", MHA),
    ("MH_15", "per 100,000", "country_reported", MHA),
    ("MH_16", "per 100,000", "country_reported", MHA),
    ("MH_17", "per 100,000", "country_reported", MHA),
    ("MH_18", "per 100,000", "country_reported", MHA),
    ("MH_19", "per 100,000", "country_reported", MHA),
    ("MH_20", "per 100,000", "country_reported", MHA),
    ("MH_24", "per 100,000", "country_reported", MHA),
    ("GDO_q35", "%", "who_comparable_estimate", "WHO estimate published in the GHO (method not stated per row)"),
    ("RSUD_1", "%", "who_comparable_estimate", "WHO estimate published in the GHO (method not stated per row)"),
    ("RSUD_2", "%", "who_comparable_estimate", "WHO estimate published in the GHO (method not stated per row)"),
)


def indicators() -> tuple[Any, ...]:
    from reva.adapters.connectors.atlas_who import WhoIndicator

    return tuple(WhoIndicator(code, unit, cls, producer) for code, unit, cls, producer in _SPEC)


def items(countries: tuple[str, ...] | None = None) -> tuple[FetchItem, ...]:
    from reva.adapters.connectors.atlas_who import INDICATOR_LIST_URL, MAX_ITEM_BYTES, data_url

    out = [FetchItem(key="gho.indicator_list", url=INDICATOR_LIST_URL, max_bytes=5_000_000)]
    for ind in indicators():
        out.append(FetchItem(key=f"gho.data:{ind.code}", url=data_url(ind.code, countries), max_bytes=MAX_ITEM_BYTES))
    return tuple(out)


def frame(bodies: dict[str, bytes], shas: dict[str, str]) -> tuple[Any, dict[str, Any]]:
    import polars as pl

    from reva.adapters.connectors.atlas_who import atlas_rows, parse_data, parse_indicator_list
    from reva.atlas.tables import ATLAS_SCHEMA

    names = parse_indicator_list(bodies["gho.indicator_list"])
    hints = concept_hints(CATALOG_FILE, "who", DATASET)
    parts, counts, empty = [], {}, []
    for ind in indicators():
        name = names.get(ind.code)
        if name is None or name.lower().startswith("archived"):
            raise err(f"{ind.code}: not (or no longer) a current GHO indicator ({name!r})")
        key = f"gho.data:{ind.code}"
        rows = parse_data(bodies[key], ind.code)
        if not rows:
            empty.append(ind.code)  # reported; no stand-in rows
            continue
        recs = atlas_rows(rows, ind, raw_sha256=shas[key], item_key=key, indicator_name=name)
        for r in recs:
            r["dataset"] = DATASET
            r["concept_id"] = hints.get(r["indicator"])
            f = json.loads(r["flags"])
            f["indicator_name"] = name
            f["classified_by"] = CLASSIFIED_BY
            r["flags"] = canon(f)
        counts[ind.code] = len(recs)
        parts.append(pl.DataFrame(recs, schema=ATLAS_SCHEMA))
    if not parts:
        raise err("no WHO mental health indicator returned rows")
    return pl.concat(parts, how="vertical"), {
        "rows": counts,
        "empty_indicators": empty,
        "indicator_names": {i.code: names[i.code] for i in indicators()},
    }


def atlas_source() -> AtlasSourceSpec:
    from reva.adapters.connectors.atlas_who import HOST, MAX_ITEM_BYTES, RIGHTS

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key="who.gho",
        budget_key="who.gho",
        atlas_source="who",
        provider="World Health Organization (Global Health Observatory: Mental Health Atlas and mental health estimates)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=tuple(i.code for i in indicators()),
        cadence_seconds=90 * 86400,
        planned_requests=len(items()),
        max_response_bytes=MAX_ITEM_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION + "+atlas-who-gho-parse-1",
        code_digest=module_digest(frame),
        field_mappings={"value": "NumericValue", "units": {i.code: i.unit for i in indicators()}},
        notes="WHO GHO mental health indicators, country rows only; parsed with atlas_who's GHO rules.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_who import RIGHTS

    if not run.wants(DATASET):
        return {"skipped": True}
    live = run.origin == "live"
    got = await fetch_archive(run, items(None if live else FIXTURE_COUNTRIES))
    tbl, rep = frame(got.bodies, got.shas)
    note = (
        "WHO GHO mental health: Mental Health Atlas country questionnaire items (MH_*; policy/plan/legislation answers "
        "kept as text, workforce/facilities/beds per 100,000), depression prevalence estimate (GDO_q35), alcohol and "
        "drug use disorder prevalence by sex (RSUD_1/RSUD_2); country rows, "
        "parsed with A-HEALTHEDU's GHO rules. Empty indicators: "
        + (", ".join(rep["empty_indicators"]) or "none")
        + "."
        + ("" if live else f" Fixture subset {','.join(FIXTURE_COUNTRIES)}.")
    )
    data_shas = sorted({got.shas[k] for k in got.shas if k.startswith("gho.data:")})
    reg = run.register(
        dataset=DATASET, frame=tbl, raw_sha256s=data_shas, coverage_note=note, geo_scheme="iso3",
        rights={**RIGHTS, "indicator_list_raw_sha256": got.shas["gho.indicator_list"], "agent": "V4MIND"},
    )  # fmt: skip
    return {"parse": rep, "atlas": reg}


__all__ = ["CONNECTOR_NAME", "DATASET", "atlas_ingest", "atlas_source", "frame", "indicators", "items"]
