"""Catalog of the World Bank atlas layer: ``registry/atlas/catalog/worldbank.yaml`` (agent A-WB).

One provider-declared concept per World Bank series code (``wb.<code lower-case>``) and one per
folded PIP indicator, built only from archived provider metadata:

* English label, definition, unit, topic and licence from the Series CSV inside each bulk ZIP
  (the first database in :data:`DB_ORDER` that reports the code is the *primary* one).
* French label from ``api.worldbank.org/v2/fr/source/<id>/indicator`` when the World Bank
  provides a translation (text differing from the English label). Where it does not, the
  catalog schema (``labels.fr`` required) gets the **English text unchanged** and the tag
  ``fr_label:missing`` -- never a machine translation. A handful of labels for the owner's
  key concepts were written by agent A-WB and are tagged ``fr_label:agent_a_wb``.
* REVA domain from a fixed, reviewed-in-code mapping (series-code prefix first, then the
  database, then the WB topic), recorded as ``subdomain`` and tag ``reva_domain:<d>``: the
  catalog schema makes a file's ``domain`` equal its file name, so every concept of
  ``worldbank.yaml`` has ``domain: worldbank``.
* ``source_order``: every World Bank database in this layer that carries the same code under
  the same indicator name (case/space-insensitive), most recently released first; a database
  that uses the code under another name is named in ``notes`` instead (not mixed).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from reva.adapters.connectors.atlas_worldbank import (
    DATABASES_BY_KEY,
    PIP_DIST_MEASURES,
    PIP_LINE_MEASURES,
    PIP_POVERTY_LINES,
    PIP_UNITS,
    PIP_VERSION,
    SOURCE,
    SeriesMeta,
    pip_concept_id,
)

CATALOG_DOMAIN = "worldbank"
CONCEPT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,199}$")
MAX_DEFINITION = 1000

#: Databases in best-available order (most recent release first; EdStats bulk is the 2018 release).
DB_ORDER = ("wdi", "gender", "wgi", "hnp", "ids", "jobs", "doingbusiness", "edstats")

UNSTATED_LICENCE = (
    "not stated in the series metadata (World Bank datasets default to CC BY 4.0 under the World Bank "
    "Terms of Use for Datasets unless otherwise noted; not re-verified for this database)"
)

REVA_DOMAINS = (
    "agriculture", "business", "economy", "education", "energy", "environment", "finance", "gender",
    "geography", "governance", "health", "infrastructure", "labour", "population", "poverty", "science",
    "social_protection", "trade",
)  # fmt: skip

#: Series-code prefix -> REVA domain (longest prefix wins). World Bank codes are topical
#: (``SP.POP`` population, ``NY.GDP`` national accounts, ``SL`` labour, ...).
PREFIX_DOMAINS: Mapping[str, str] = {
    "SP.POP.": "population", "SP.DYN.CBRT": "population", "SP.DYN.CDRT": "population",
    "SP.DYN.TFRT": "population", "SP.DYN.WFRT": "population", "SP.DYN.LE": "health", "SP.DYN.AMRT": "health",
    "SP.DYN.IMRT": "health", "SP.DYN.TO65": "health", "SP.DYN.CONU": "health", "SP.URB.": "population",
    "SP.RUR.": "population", "SM.POP.": "population", "SP.ADO.": "health", "SP.REG.": "population",
    "SP.MTR.": "health", "SP.HOU.": "population", "SP.M": "gender", "SP.UWT.": "gender",
    "EN.POP.DNST": "population", "EN.URB.": "population", "AG.LND.TOTL.K2": "geography",
    "AG.SRF.TOTL.K2": "geography", "AG.LND.TOTL.UR.K2": "geography", "AG.": "agriculture",
    "EG.": "energy", "EN.": "environment", "ER.": "environment", "SL.": "labour", "SE.": "education",
    "SH.": "health", "NY.": "economy", "NV.": "economy", "NE.EXP.": "trade", "NE.IMP.": "trade",
    "NE.RSB.": "trade", "NE.TRD.": "trade", "NE.": "economy", "FP.": "economy", "PA.": "economy",
    "PX.": "economy", "BX.": "trade", "BM.": "trade", "BN.": "trade", "TX.": "trade", "TM.": "trade",
    "TG.": "trade", "ST.INT.": "trade", "DT.": "finance", "FB.": "finance", "FD.": "finance",
    "FI.": "finance", "FM.": "finance", "FR.": "finance", "FS.": "finance", "CM.": "finance",
    "GC.": "economy", "IC.": "business", "IE.": "infrastructure", "IS.": "infrastructure",
    "IT.": "infrastructure", "LP.": "trade", "GB.": "science", "IP.": "science", "SI.POV.": "poverty",
    "SI.DST.": "poverty", "SI.SPR.": "poverty", "SI.RMT.": "finance", "PER_": "social_protection",
    "SG.": "gender", "MS.MIL.": "governance", "VC.": "governance", "IQ.": "governance",
    "GOV_WGI_": "governance", "CC.": "governance", "GE.": "governance", "PV.": "governance",
    "RL.": "governance", "RQ.": "governance", "VA.": "governance", "SN.ITK.": "health",
    "SP.": "population",
}  # fmt: skip

#: Database default domain (topic-agnostic databases), applied after the prefix rules.
DB_DOMAINS: Mapping[str, str] = {
    "wgi": "governance", "doingbusiness": "business", "ids": "finance", "edstats": "education",
    "gender": "gender",
}  # fmt: skip

#: WB topic (first segment) -> REVA domain, the last rule.
TOPIC_DOMAINS: Mapping[str, str] = {
    "Economic Policy & Debt": "economy", "Health": "health", "Environment": "environment",
    "Social Protection & Labor": "labour", "Education": "education", "Private Sector & Trade": "trade",
    "Financial Sector": "finance", "Public Sector": "governance", "Infrastructure": "infrastructure",
    "Trade": "trade", "Policy & institutions": "governance", "Poverty": "poverty", "Gender": "gender",
}  # fmt: skip

#: French labels written by agent A-WB for key concepts the World Bank leaves untranslated
#: (tag ``fr_label:agent_a_wb``). Plain terminology only.
AGENT_FR: Mapping[str, str] = {
    "SP.POP.TOTL": "Population totale",
    "SP.POP.TOTL.FE.IN": "Population féminine",
    "SP.POP.TOTL.MA.IN": "Population masculine",
    "SP.URB.TOTL": "Population urbaine",
    "SP.RUR.TOTL": "Population rurale",
    "GOV_WGI_CC.EST": "Contrôle de la corruption : estimation (env. -2,5 à +2,5)",
    "GOV_WGI_GE.EST": "Efficacité des pouvoirs publics : estimation (env. -2,5 à +2,5)",
    "GOV_WGI_PV.EST": "Stabilité politique et absence de violence/terrorisme : estimation (env. -2,5 à +2,5)",
    "GOV_WGI_RL.EST": "État de droit : estimation (env. -2,5 à +2,5)",
    "GOV_WGI_RQ.EST": "Qualité de la réglementation : estimation (env. -2,5 à +2,5)",
    "GOV_WGI_VA.EST": "Voix citoyenne et responsabilité : estimation (env. -2,5 à +2,5)",
}

PIP_LABELS: Mapping[str, tuple[str, str]] = {
    "headcount": ("Poverty headcount ratio", "Taux de pauvreté (part de la population)"),
    "poverty_gap": ("Poverty gap index", "Indice d'écart de pauvreté"),
    "poverty_severity": ("Poverty severity index (squared gap)", "Indice de sévérité de la pauvreté"),
    "watts": ("Watts poverty index", "Indice de pauvreté de Watts"),
    "pop_in_poverty": ("Population living in poverty", "Population vivant dans la pauvreté"),
    "mean": ("Mean welfare per day", "Bien-être moyen par jour"),
    "median": ("Median welfare per day", "Bien-être médian par jour"),
    "mld": ("Mean log deviation", "Écart logarithmique moyen"),
    "gini": ("Gini index", "Indice de Gini"),
    "polarization": ("Polarization index", "Indice de polarisation"),
    "spl": ("Societal poverty line", "Seuil de pauvreté sociétal"),
    "spr": ("Societal poverty rate", "Taux de pauvreté sociétal"),
    "pg": ("Prosperity gap", "Écart de prospérité"),
    "cpi": ("Consumer price index used by PIP", "Indice des prix à la consommation utilisé par PIP"),
    "ppp": ("PPP conversion factor used by PIP", "Facteur de conversion PPA utilisé par PIP"),
    "reporting_pop": ("Population used by PIP", "Population utilisée par PIP"),
    "reporting_gdp": ("GDP per capita used by PIP", "PIB par habitant utilisé par PIP"),
    "reporting_pce": ("Household consumption per capita used by PIP", "Consommation des ménages par habitant utilisée par PIP"),
    **{f"decile{i}": (f"Welfare share of decile {i}", f"Part du bien-être du décile {i}") for i in range(1, 11)},
}  # fmt: skip


def concept_id(code: str) -> str | None:
    cid = "wb." + code.lower()
    return cid if CONCEPT_RE.match(cid) else None


def reva_domain(code: str, db: str, topic: str) -> str:
    best = ""
    for p in PREFIX_DOMAINS:
        if code.startswith(p) and len(p) > len(best):
            best = p
    if best:
        return PREFIX_DOMAINS[best]
    if db in DB_DOMAINS:
        return DB_DOMAINS[db]
    return TOPIC_DOMAINS.get(topic.split(":")[0].strip(), "economy" if db == "wdi" else "other")


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _slug(s: str, n: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:n] or "none"


def _definition(m: SeriesMeta) -> tuple[str, bool]:
    text = m.long_definition or m.short_definition or m.name
    if len(text) > MAX_DEFINITION:
        return text[: MAX_DEFINITION - 1].rstrip() + "…", True
    return text, False


def _indicator(db: str, code: str) -> str:
    return f"{code}|counterpart=WLD" if db == "ids" else code


def build_catalog(
    series: Mapping[str, Mapping[str, SeriesMeta]],
    units: Mapping[str, Mapping[str, tuple[str, str]]],
    datasets: Mapping[str, Mapping[str, str]],
    fr_labels: Mapping[str, Mapping[str, Mapping[str, str]]],
    *,
    raw_sha256s: Mapping[str, str],
    pip_indicators: Sequence[str] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(catalog_doc, stats)``.

    ``series[db][code]`` provider metadata; ``units[db][code]`` (unit, how) of the codes with kept
    cells; ``datasets[db][code]`` the atlas dataset of each code (WDI split); ``fr_labels[db][code]``
    the World Bank's French metadata; ``raw_sha256s[db]`` the archived bulk ZIP of each database."""
    concepts: list[dict[str, Any]] = []
    stats: dict[str, Any] = {"by_domain": {}, "fr": {"provider": 0, "agent_a_wb": 0, "missing": 0}, "skipped": []}
    codes = sorted({c for db in units for c in units[db] if db != "pip"})
    for code in codes:
        cid = concept_id(code)
        dbs = [db for db in DB_ORDER if code in units.get(db, {})]
        if cid is None or not dbs:
            stats["skipped"].append(code)
            continue
        primary = dbs[0]
        meta = series.get(primary, {}).get(code) or next(
            (series[d][code] for d in dbs if code in series.get(d, {})), None
        )
        name = meta.name if meta and meta.name else code
        same, other = [], []
        for db in dbs:
            m = series.get(db, {}).get(code)
            if db == primary or (m is not None and _norm(m.name) == _norm(name)) or m is None:
                same.append(db)
            else:
                other.append(f"{DATABASES_BY_KEY[db].title} names it '{m.name}'")
        source_order = [f"{SOURCE}.{datasets[db][code]}.{_indicator(db, code)}" for db in same]
        # French label: World Bank translation (any database carrying the code), else agent, else missing.
        fr, fr_tag = None, "missing"
        for db in [primary, *dbs[1:]]:
            f = (fr_labels.get(db) or {}).get(code)
            if f and f.get("name") and _norm(f["name"]) != _norm(name):
                fr, fr_tag = f["name"], "provider"
                break
        if fr is None and code in AGENT_FR:
            fr, fr_tag = AGENT_FR[code], "agent_a_wb"
        stats["fr"][fr_tag] += 1
        topic = meta.topic if meta else ""
        dom = reva_domain(code, primary, topic)
        stats["by_domain"][dom] = stats["by_domain"].get(dom, 0) + 1
        definition, truncated = _definition(meta) if meta else ("", False)
        no_definition = not definition
        if no_definition:
            definition = f"{name} (the World Bank metadata gives no definition for {code})."
        unit, unit_how = units[primary][code]
        licence = (meta.licence if meta and meta.licence else "") or UNSTATED_LICENCE
        notes = [
            f"Provider metadata: World Bank {DATABASES_BY_KEY[primary].title}, series {code}"
            f" (bulk ZIP sha256 {raw_sha256s.get(primary, '?')[:16]}…).",
            f"Licence: {licence}.",
        ]
        if meta and meta.source_note:
            notes.append(f"Upstream source: {meta.source_note[:600]}")
        if meta and meta.periodicity:
            notes.append(f"Periodicity: {meta.periodicity}.")
        if unit_how != "provider_unit_of_measure":
            notes.append(f"Unit taken from: {unit_how.replace('_', ' ')}.")
        if no_definition:
            notes.append("Provider metadata has no definition text; the definition repeats the indicator name.")
        if truncated:
            notes.append("Definition truncated; the full text is in the archived Series metadata.")
        if other:
            notes.append("Not merged (different indicator name): " + "; ".join(other)[:800] + ".")
        if fr_tag == "missing":
            notes.append("No French label provided by the World Bank: labels.fr repeats the English text.")
        if fr_tag == "agent_a_wb":
            notes.append("French label written by agent A-WB (not a World Bank translation).")
        for db in dbs:
            if DATABASES_BY_KEY[db].note and db in same:
                notes.append(f"{DATABASES_BY_KEY[db].title}: {DATABASES_BY_KEY[db].note}")
        tags = [
            f"reva_domain:{dom}",
            f"wb_topic:{_slug(topic.split(':')[0]) if topic else 'none'}",
            f"fr_label:{fr_tag}",
            *(f"wb_db:{db}" for db in same),
        ]
        if not (meta and meta.licence.upper().startswith("CC BY")):
            tags.append("licence:check")
        concepts.append(
            {
                "id": cid,
                "labels": {"en": name[:300], "fr": (fr or name)[:300]},
                "definition": definition,
                "unit": unit[:100],
                "domain": CATALOG_DOMAIN,
                "subdomain": dom,
                "value_kind": "numeric",
                "source_order": source_order,
                "provider_declared": True,
                "notes": " ".join(notes)[:4000],
                "tags": tags[:30],
            }
        )
    for ind in sorted(pip_indicators):
        measure, *dims = ind.split("|")
        d = dict(x.split("=", 1) for x in dims)
        line = d.get("povline")
        cid = pip_concept_id(ind)
        en, fr = PIP_LABELS.get(measure, (measure, measure))
        qual_en = (f" at ${line}/day (2021 PPP)" if line else "") + f", {d['level']}, {d['welfare']}-based"
        qual_fr = (f" au seuil de {line.replace('.', ',')} $/jour (PPA 2021)" if line else "") + (
            f", {d['level']}, mesure fondée sur {'la consommation' if d['welfare'] == 'consumption' else 'le revenu'}"
        )
        stats["fr"]["agent_a_wb"] += 1
        stats["by_domain"]["poverty"] = stats["by_domain"].get("poverty", 0) + 1
        concepts.append(
            {
                "id": cid,
                "labels": {"en": (en + qual_en)[:300], "fr": (fr + qual_fr)[:300]},
                "definition": (
                    f"Poverty and Inequality Platform '{measure}' as reported by the World Bank PIP API "
                    f"(release {PIP_VERSION}), survey-year estimates only (fill_gaps=false), reporting level "
                    f"'{d['level']}', welfare type '{d['welfare']}'"
                    + (f", international poverty line {line} US$ per day (2021 PPP)." if line else ".")
                ),
                "unit": PIP_UNITS.get(measure, "unspecified"),
                "domain": CATALOG_DOMAIN,
                "subdomain": "poverty",
                "value_kind": "numeric",
                "source_order": [f"{SOURCE}.pip.{ind}"],
                "provider_declared": True,
                "notes": (
                    "Labels written by agent A-WB from the PIP column names (PIP publishes no label metadata). "
                    "Licence: CC BY 4.0 (World Bank PIP). Income- and consumption-based estimates are separate "
                    "concepts and are not comparable without care."
                ),
                "tags": ["reva_domain:poverty", "wb_db:pip", "fr_label:agent_a_wb"],
            }
        )
    doc = {
        "domain": CATALOG_DOMAIN,
        "labels": {"fr": "Banque mondiale (toutes bases)", "en": "World Bank (all databases)"},
        "maintainer": "agent A-WB (generated by reva.adapters.connectors.atlas_worldbank_catalog)",
        "concepts": concepts,
    }
    stats["concepts"] = len(concepts)
    stats["raw_sha256s"] = dict(raw_sha256s)
    return doc, stats


PIP_ALL_MEASURES = PIP_LINE_MEASURES + PIP_DIST_MEASURES


def archived_raw(engine: Any, *, origin: str = "live") -> dict[str, str]:
    """Item key -> raw sha256 of the latest archived acquisition of each ``atlas.worldbank`` item."""
    from sqlalchemy import text

    from reva.adapters.connectors.atlas_worldbank import CONNECTOR_NAME

    with engine.connect() as c:
        rows = c.execute(
            text(
                """SELECT DISTINCT ON (a.response_metadata->>'item_key') a.response_metadata->>'item_key', a.raw_sha256
                     FROM evidence.acquisition a JOIN evidence.connector_version cv ON cv.id = a.connector_version_id
                    WHERE cv.name = :n AND a.origin = :o
                    ORDER BY a.response_metadata->>'item_key', a.recorded_at DESC, a.id DESC"""
            ),
            {"n": CONNECTOR_NAME, "o": origin},
        ).all()
    return {str(k): str(v) for k, v in rows}


def catalog_from_archive(engine: Any, store: Any, *, origin: str = "live") -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild the catalog from the archived raw bytes (re-parses each bulk ZIP; ~3 minutes, ~5 GB peak)."""
    import gc
    import hashlib

    from reva.adapters.connectors import atlas_worldbank as wb

    raws = archived_raw(engine, origin=origin)

    def get(key: str) -> tuple[bytes, str]:
        sha = raws[key]
        data = store.get("raw", sha)
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"archived bytes of {key} do not match their sha256")
        return data, sha

    series, units, datasets, fr, shas = {}, {}, {}, {}, {}
    for db in wb.DATABASES:
        if f"bulk:{db.key}" not in raws:
            continue
        body, sha = get(f"bulk:{db.key}")
        p = wb.parse_bulk(db, body, sha)
        series[db.key], units[db.key], datasets[db.key], shas[db.key] = p.series, p.units, p.datasets, sha
        del p, body
        gc.collect()
        if f"fr:{db.key}" in raws:
            fr[db.key] = wb.parse_fr_labels(get(f"fr:{db.key}")[0])
    pip_inds: list[str] = []
    if all(f"pip:{line}" in raws for line in PIP_POVERTY_LINES):
        pip = wb.parse_pip({line: get(f"pip:{line}") for line in PIP_POVERTY_LINES})
        pip_inds = sorted(pip.units)
    return build_catalog(series, units, datasets, fr, raw_sha256s=shas, pip_indicators=pip_inds)


def dump_catalog(doc: dict[str, Any]) -> str:
    import yaml

    header = (
        "# World Bank atlas concepts (agent A-WB). GENERATED by\n"
        "#   uv run python -m reva.adapters.connectors.atlas_worldbank_catalog\n"
        "# from the archived raw bytes of the atlas.worldbank connector (bulk ZIP Series metadata, French\n"
        "# indicator lists, PIP columns). Provider-declared, not human-reviewed. domain = worldbank (catalog\n"
        "# rule: domain == file name); the REVA domain is `subdomain` and tag `reva_domain:<d>`.\n"
        "# labels.fr: World Bank translation (tag fr_label:provider), agent A-WB (fr_label:agent_a_wb) or the\n"
        "# English text unchanged when the World Bank gives none (fr_label:missing) -- never machine-translated.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=4096)


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - operator entry point
    import argparse
    import json

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.atlas.catalog import CATALOG_DIR
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description="Rebuild registry/atlas/catalog/worldbank.yaml from the archive.")
    ap.add_argument("--origin", default="live", choices=["live", "test_fixture"])
    ap.add_argument("--out", default=str(CATALOG_DIR / "worldbank.yaml"))
    args = ap.parse_args(argv)
    settings = get_settings()
    doc, stats = catalog_from_archive(make_engine("worker", settings), get_store(), origin=args.origin)
    from pathlib import Path

    Path(args.out).write_text(dump_catalog(doc), encoding="utf-8")
    print(json.dumps({k: v for k, v in stats.items() if k != "skipped"}, indent=1))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "DB_ORDER",
    "PIP_POVERTY_LINES",
    "build_catalog",
    "catalog_from_archive",
    "concept_id",
    "dump_catalog",
    "reva_domain",
]
