"""Statistical regions (Eurostat NUTS, OECD territorial levels) as KG entities (V4, agent V4ECON).

The regional atlas tables (``eurostat.nuts_*`` from :mod:`atlas_eurostat_macro`, ``oecd.reg_*`` from
:mod:`atlas_oecd_macro`) carry one row per region code. This module turns the region codes those responses
actually contain into KG entities and three kinds of edges, every one read from an identifier the provider
publishes (never from a name):

* ``<ns>:part_of`` (family ``part_of``) — the parent region:
  - NUTS: the code with its last character removed (``FR101`` -> ``FR10`` -> ``FR1`` -> ``FR``), the NUTS
    coding rule of Regulation (EC) No 1059/2003 (each level adds one character to the code of the level
    above), kept only when the parent code is itself in the provider's response;
  - OECD: the ``Parent`` the OECD ``CL_REGIONAL`` codelist declares for the code (SDMX ``<str:Parent>``).
* ``<ns>:in_country`` (family ``located_in``) — the country, as a Wikidata item from
  ``registry/entities/country_qids.json``: NUTS through the code's two-letter country prefix (Eurostat
  country codes: ``EL`` Greece, ``UK`` United Kingdom; crosswalk scheme ``eurostat``); OECD through the
  ``COUNTRY`` attribute of the data rows (ISO 3166-1 alpha-3).
* ``econ:measured_by`` (family ``measured_by``) — the atlas table that holds the region's series
  (object = a ``data_series`` entity ``atlas:<source>.<dataset>``).

Entity ids are namespaced provider codes: ``nuts:FR101`` and ``oecdreg:FR101`` stay **different** entities
even when the code is the same (the OECD TL3 of a European country is usually, not always, the NUTS 3 region
of the same code; neither source publishes the equivalence, so REVA does not assert it). A NUTS code can
denote different territories in different NUTS versions; the responses do not state the version per cell, so
each entity description names the dataset and its update date it was read from.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from reva.atlas.tables import country_iso3

#: (pid, value kind, reva predicate, label en, label fr, inverse name, inverse label en)
PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "nuts:part_of": (
        "item",
        "nuts_part_of",
        "NUTS parent region (part of)",
        "région NUTS parente (fait partie de)",
        "nuts_has_part",
        "NUTS subregion",
    ),
    "nuts:in_country": (
        "item",
        "nuts_in_country",
        "country of the NUTS region",
        "pays de la région NUTS",
        "nuts_region_of_country",
        "NUTS region",
    ),
    "nuts:level": ("string", "nuts_level", "NUTS level (from the code length)", "niveau NUTS", None, None),
    "oecdreg:part_of": (
        "item",
        "oecdreg_part_of",
        "OECD parent territory (CL_REGIONAL parent)",
        "territoire OCDE parent (CL_REGIONAL)",
        "oecdreg_has_part",
        "OECD sub-territory",
    ),
    "oecdreg:in_country": (
        "item",
        "oecdreg_in_country",
        "country of the OECD region (COUNTRY attribute)",
        "pays de la région OCDE (attribut COUNTRY)",
        "oecdreg_region_of_country",
        "OECD region",
    ),
    "oecdreg:territorial_level": (
        "string",
        "oecdreg_territorial_level",
        "OECD territorial level (TL2 / TL3)",
        "niveau territorial OCDE (TL2 / TL3)",
        None,
        None,
    ),
    "econ:measured_by": (
        "item",
        "econ_measured_by",
        "measured by (atlas data series)",
        "mesuré par (série de l'atlas)",
        "econ_measures",
        "measures",
    ),
}

NAMESPACES = {"nuts": "nuts_region", "oecdreg": "oecd_region"}


@dataclass
class Region:
    """One region code seen in a provider response."""

    ns: str  # "nuts" | "oecdreg"
    code: str
    label_en: str
    level: str  # "NUTS 2", "TL3", ...
    country_iso3: str | None
    parent: str | None = None  # parent code in the same namespace (None: top level or unknown)
    parent_basis: str = ""
    country_basis: str = ""
    tables: set[str] = field(default_factory=set)  # atlas table keys "eurostat.nuts_gdp"
    described_by: str = ""  # dataset + update date the label was read from
    raw_sha256: str = ""
    selector: dict[str, Any] = field(default_factory=dict)

    @property
    def qid(self) -> str:
        return f"{self.ns}:{self.code}"


# --------------------------------------------------------------------------- NUTS
NUTS_BASIS = (
    "NUTS code hierarchy: each NUTS level adds one character to the code of the level above (Regulation (EC)"
    " No 1059/2003, Article 3 and Annex); parent kept only when the parent code is in the same Eurostat response"
)
NUTS_COUNTRY_BASIS = "two-letter country prefix of the NUTS code (Eurostat country code; crosswalk scheme 'eurostat')"


def nuts_country(code: str) -> str | None:
    """ISO3 of the NUTS code's country prefix (``EL`` -> GRC, ``UK`` -> GBR), ``None`` for aggregates."""
    if len(code) < 2 or not code[:2].isalpha() or not code[:2].isupper():
        return None
    return country_iso3(code[:2], "eurostat")


def is_nuts_region(code: str) -> bool:
    """A NUTS 1-3 code (3-5 characters, letters/digits, country prefix that maps to a country)."""
    return 3 <= len(code) <= 5 and code.isalnum() and code.isupper() and nuts_country(code) is not None


def nuts_regions(
    labels: Mapping[str, str], *, table: str, described_by: str, raw_sha256: str, dataset_code: str
) -> dict[str, Region]:
    """NUTS 1-3 regions of one Eurostat response (``labels`` = the response's ``geo`` code -> label)."""
    out: dict[str, Region] = {}
    for code, label in labels.items():
        if not is_nuts_region(code):
            continue
        parent = code[:-1]
        has_parent = parent in labels and (len(parent) == 2 or is_nuts_region(parent))
        out[code] = Region(
            ns="nuts",
            code=code,
            label_en=label,
            level=f"NUTS {len(code) - 2}",
            country_iso3=nuts_country(code),
            parent=parent if has_parent and len(parent) > 2 else None,
            parent_basis=NUTS_BASIS if has_parent else "",
            country_basis=NUTS_COUNTRY_BASIS,
            tables={table},
            described_by=described_by,
            raw_sha256=raw_sha256,
            selector={"dataset": dataset_code, "dimension": "geo", "code": code},
        )
    return out


# --------------------------------------------------------------------------- merge + build
def merge(regions: Iterable[Mapping[str, Region]]) -> dict[str, Region]:
    """Union of several responses' regions (first label wins; tables are united)."""
    out: dict[str, Region] = {}
    for rs in regions:
        for r in rs.values():
            cur = out.get(r.qid)
            if cur is None:
                out[r.qid] = Region(**{**r.__dict__, "tables": set(r.tables), "selector": dict(r.selector)})
            else:
                cur.tables |= r.tables
                if cur.parent is None and r.parent is not None:
                    cur.parent, cur.parent_basis = r.parent, r.parent_basis
    return out


def build(
    regions: Mapping[str, Region],
    country_qids: Mapping[str, str],
    *,
    source_key: str,
    revision: str,
    retrieved_at: datetime,
    licence: str,
    table_titles: Mapping[str, str],
) -> tuple[list[Any], list[Any], dict[str, int]]:
    """KG entities and statements for ``regions`` (``country_qids``: ISO3 -> Wikidata QID)."""
    from reva.kg.model import KgEntity, KgStatement

    ents: list[Any] = []
    sts: list[Any] = []
    info: dict[str, int] = {"regions": 0, "part_of": 0, "in_country": 0, "measured_by": 0, "country_qid_missing": 0}
    tables: set[str] = set()

    def ref(r: Region) -> list[dict[str, Any]]:
        return [
            {
                "source_key": source_key,
                "raw_sha256": r.raw_sha256,
                "selector": r.selector,
                "described_by": r.described_by,
                "retrieved_at": retrieved_at.isoformat(),
                "licence": licence,
            }
        ]

    for r in sorted(regions.values(), key=lambda x: x.qid):
        info["regions"] += 1
        ents.append(
            KgEntity(
                qid=r.qid,
                kind=NAMESPACES[r.ns],
                labels={"en": r.label_en[:480]},
                descriptions={"en": f"{r.level} statistical region {r.code} ({r.described_by})"[:480]},
                source_revision=revision,
            )
        )

        def st(pid: str, *, obj: str | None = None, value: Mapping[str, Any] | None = None, **q: Any) -> None:
            ident = obj or (value or {}).get("value", "")
            sid = hashlib.sha256(f"{r.qid}|{pid}|{ident}".encode()).hexdigest()[:24]
            sts.append(
                KgStatement(
                    subject_qid=r.qid,
                    property_pid=pid,
                    object_qid=obj,
                    value=value,
                    qualifiers=dict(q),
                    references=ref(r),
                    source_key=source_key,
                    source_revision=revision,
                    statement_id=f"v4econ:{sid}",
                    raw_sha256=r.raw_sha256,
                )
            )

        lvl_pid = "nuts:level" if r.ns == "nuts" else "oecdreg:territorial_level"
        st(lvl_pid, value={"type": "string", "value": r.level})
        if r.parent:
            st(f"{r.ns}:part_of", obj=f"{r.ns}:{r.parent}", basis=r.parent_basis)
            info["part_of"] += 1
        cq = country_qids.get(r.country_iso3 or "")
        if cq:
            st(f"{r.ns}:in_country", obj=cq, country_iso3=r.country_iso3, basis=r.country_basis)
            info["in_country"] += 1
        elif r.country_iso3:
            info["country_qid_missing"] += 1
        for t in sorted(r.tables):
            st("econ:measured_by", obj=f"atlas:{t}", atlas_table=t)
            info["measured_by"] += 1
            tables.add(t)
    for t in sorted(tables):
        ents.append(
            KgEntity(
                qid=f"atlas:{t}",
                kind="data_series",
                labels={"en": f"atlas table {t}: {table_titles.get(t, '')}"[:480]},
                descriptions={"en": f"REVA atlas table {t} (values are read from the atlas, not the KG)"[:480]},
                source_revision=revision,
            )
        )
    return ents, sts, info


def kg_properties() -> list[Any]:
    from reva.kg.model import KgProperty

    return [
        KgProperty(
            pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv, inverse_label_en=ile
        )
        for p, (k, rp, le, lf, inv, ile) in PROPERTIES.items()
    ]


def load(
    engine: Any,
    store: Any,
    regions: Mapping[str, Region],
    *,
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    licence: str,
    table_titles: Mapping[str, str],
    connector: str,
    country_qids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Register the predicates and load the region entities/statements (``load_batch``, bounded)."""
    from reva.adapters.connectors.atlas_geo_common import load_country_qids
    from reva.kg.model import MAX_STATEMENTS_PER_BATCH, load_batch, register_properties

    if not regions:
        return {"loaded": False, "reason": "no region codes in the responses"}
    cq = dict(country_qids) if country_qids is not None else load_country_qids()
    revision = hashlib.sha256("|".join(sorted(raw_sha256s)).encode()).hexdigest()[:16]
    ents, sts, info = build(
        regions,
        cq,
        source_key=source_key,
        revision=revision,
        retrieved_at=retrieved_at,
        licence=licence,
        table_titles=table_titles,
    )
    try:
        register_properties(engine, kg_properties())
    except Exception as e:  # a database without the kg schema: report, never fail the atlas run
        return {"loaded": False, "blocked": f"KG not writable ({type(e).__name__}: {str(e)[:200]})", **info}
    reports = []
    for i in range(0, max(len(sts), 1), MAX_STATEMENTS_PER_BATCH):
        rep = load_batch(
            engine,
            store,
            entities=ents if i == 0 else (),
            statements=sts[i : i + MAX_STATEMENTS_PER_BATCH],
            source_key=source_key,
            raw_sha256s=sorted(set(raw_sha256s)),
            retrieved_at=retrieved_at,
            origin=origin,
            parameters={"connector": connector, "layer": "v4econ statistical regions"},
        )
        d = rep.as_dict()
        reports.append({k: d[k] for k in ("load_id", "entities_inserted", "statements_inserted", "rejected_counts")})
    return {"loaded": True, **info, "entities": len(ents), "statements": len(sts), "reports": reports}


__all__ = [
    "NAMESPACES",
    "PROPERTIES",
    "Region",
    "build",
    "is_nuts_region",
    "kg_properties",
    "load",
    "merge",
    "nuts_country",
    "nuts_regions",
]
