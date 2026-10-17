"""CODATA recommended values of the fundamental physical constants (NIST) -> atlas + KG (V4, agent V4CHEM).

Source
------
``https://physics.nist.gov/cuu/Constants/Table/allascii.txt`` — "Fundamental Physical Constants --- Complete
Listing", one fixed-width line per quantity (Quantity, Value, Uncertainty, Unit); the header names the adjustment
("2022 CODATA adjustment"). One request (~41 kB).

Licence: NIST Standard Reference Data (SRD 121). NIST states that its SRD may be protected by copyright in the US
under 15 U.S.C. 290e and asks for citation ("Source: NIST", https://www.nist.gov/open/license); recorded as
``licence_review`` (owner decision before redistribution), not as public domain.

Parsing (verbatim, no rounding)
-------------------------------
* digit-group spaces are removed (``6.022 140 76 e23`` -> 6.02214076e23);
* ``(exact)`` -> flag ``exact``; a value ending in ``...`` is an exact value the table truncates: flag
  ``truncated_by_source`` (the printed digits are kept, nothing is appended);
* the uncertainty is the standard uncertainty in the same unit and power of ten as printed.

Published: atlas ``codata.constants`` (indicator = quantity slug, provider_geo ``not_geographic``, period = the
adjustment year) and KG entities ``codata:<slug>`` (kind ``physical_constant``, label = the NIST quantity name)
with one ``codata:value`` statement (quantity; unit, uncertainty, exact, adjustment as qualifiers).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from reva.adapters.connectors.atlas_chem_common import (
    NOT_GEOGRAPHIC,
    atlas_decimal,
    atlas_frame,
    canon,
    code_digest,
    err,
    load_kg,
    quantity,
    slug,
    to_decimal,
    trim_registration,
)

CONNECTOR_NAME = "atlas.chem_codata"
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas-chem-codata-1"
SOURCE_KEY = "nist.codata"
ATLAS_SOURCE = "codata"
DATASET = "constants"
HOST = "physics.nist.gov"
URL = f"https://{HOST}/cuu/Constants/Table/allascii.txt"
MIN_CONSTANTS = 300  # 355 in the 2022 listing
KG_PREFIXES = ("codata:",)

RIGHTS: dict[str, Any] = {
    "licence": "NIST Standard Reference Data (SRD 121): cite 'Source: NIST'; possible US copyright under 15 U.S.C. 290e",
    "licence_url": "https://www.nist.gov/open/license",
    "attribution": "CODATA Internationally recommended values of the Fundamental Physical Constants, NIST (physics.nist.gov/constants)",
    "licence_review": True,
}


@dataclass(frozen=True)
class Constant:
    line: int  # 1-based line number in allascii.txt
    name: str
    value_text: str
    value: Decimal
    uncertainty_text: str
    uncertainty: Decimal | None
    exact: bool
    truncated: bool
    unit: str

    @property
    def key(self) -> str:
        return f"codata:{slug(self.name)}"


def _num(text: str) -> tuple[Decimal | None, bool]:
    """``6.022 140 76 e23`` -> Decimal; ``1.054 571 817... e-34`` -> (value as printed, truncated=True)."""
    t = text.strip()
    truncated = "..." in t
    t = t.replace("...", "").replace(" ", "")
    return to_decimal(t), truncated


def parse_listing(body: bytes, *, min_constants: int = MIN_CONSTANTS) -> tuple[str, list[Constant]]:
    """(adjustment label e.g. "2022 CODATA adjustment", constants)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    lines = text.splitlines()
    adj = next((ln.strip() for ln in lines[:10] if "CODATA adjustment" in ln), None)
    if adj is None or not re.match(r"^[0-9]{4} CODATA adjustment$", adj):
        raise err(f"CODATA listing: no adjustment header ({adj!r})")
    hdr = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("Quantity") and "Uncertainty" in ln), None)
    if hdr is None or not lines[hdr + 1].startswith("-----"):
        raise err("CODATA listing: header line not found")
    h = lines[hdr]
    c_val, c_unc, c_unit = h.index("Value"), h.index("Uncertainty"), h.index("Unit")
    # the header labels are indented relative to the data columns (2022 listing: 'Value' at 63, data at 60)
    data_starts = _column_starts(lines[hdr + 2 :], (c_val, c_unc, c_unit))
    out: list[Constant] = []
    for i in range(hdr + 2, len(lines)):
        ln = lines[i]
        if not ln.strip():
            continue
        v0, u0, n0 = data_starts
        name, vtxt, utxt, unit = ln[:v0].strip(), ln[v0:u0].strip(), ln[u0:n0].strip(), ln[n0:].strip()
        val, trunc = _num(vtxt)
        if not name or val is None:
            raise err(f"CODATA listing line {i + 1}: cannot read value {vtxt!r}")
        exact = utxt == "(exact)"
        unc = None if exact else _num(utxt)[0]
        if not exact and unc is None:
            raise err(f"CODATA listing line {i + 1}: cannot read uncertainty {utxt!r}")
        out.append(Constant(i + 1, name, vtxt, val, utxt, unc, exact, trunc, unit))
    if len(out) < min_constants:
        raise err(f"CODATA listing: only {len(out)} constants", kind="incomplete")
    keys = [c.key for c in out]
    if len(set(keys)) != len(keys):
        raise err("CODATA listing: two quantity names give the same key")
    return adj, out


def _column_starts(data: Sequence[str], header_pos: tuple[int, int, int]) -> tuple[int, int, int]:
    """Data column starts: for each header label, the left-most position at or before it where every data line
    has a blank before a non-blank (fixed-width table). Falls back to the header positions."""
    rows = [ln for ln in data if ln.strip()]
    starts = []
    for pos in header_pos:
        best = pos
        for p in range(pos, max(pos - 8, 1), -1):
            if all(len(r) <= p or (r[p - 1] == " ") for r in rows) and any(len(r) > p and r[p] != " " for r in rows):
                best = p
        starts.append(best)
    return starts[0], starts[1], starts[2]


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="NIST Physical Measurement Laboratory (CODATA Task Group on Fundamental Constants)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("cuu/Constants/Table/allascii.txt",),
        cadence_seconds=365 * 86400,
        planned_requests=2,
        max_response_bytes=2_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__], sys.modules["reva.adapters.connectors.atlas_chem_common"]),
        field_mappings={"atlas": f"{ATLAS_SOURCE}.{DATASET}", "kg": list(KG_PREFIXES)},
        notes="one request; new CODATA adjustments every 4 years",
    )


async def atlas_ingest(run: Any, *, load: bool = True) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": DATASET}
    raw = await run.get(URL, key="allascii", media_types=("text/plain",), max_bytes=2_000_000)
    sha = run.archive(raw)
    adj, consts = parse_listing(raw.body)
    year = int(adj[:4])
    reg = run.register(
        dataset=DATASET,
        frame=atlas_frame(rows(consts, adj, raw_sha256=sha, retrieved_at=raw.retrieved_at)),
        raw_sha256s=[sha],
        coverage_note=f"All {len(consts)} quantities of the NIST listing ({adj}).",
        rights=RIGHTS,
    )
    out: dict[str, Any] = {"adjustment": adj, "constants": len(consts), "atlas": trim_registration(reg)}
    if load:
        ents, stmts = kg_build(consts, adj, year, raw_sha256=sha, retrieved_at=raw.retrieved_at)
        out["kg"] = load_kg(
            run.engine,
            run.store,
            entities=ents,
            statements=stmts,
            source_key=SOURCE_KEY,
            raw_sha256s=[sha],
            retrieved_at=raw.retrieved_at,
            origin=run.origin,
            parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION, "adjustment": adj},
            prefixes=KG_PREFIXES,
            retract_prefix="",
        )
    return out


def rows(consts: Sequence[Constant], adj: str, *, raw_sha256: str, retrieved_at: datetime) -> list[dict[str, Any]]:
    year = int(adj[:4])
    out = []
    for c in consts:
        flags: dict[str, Any] = {
            "quantity": c.name,
            "adjustment": adj,
            "value_verbatim": c.value_text,
            "uncertainty_verbatim": c.uncertainty_text,
            "retrieved_at": retrieved_at.isoformat(),
            "period_semantics": "CODATA adjustment year (data up to its closing date)",
            "licence_review": True,
        }
        if c.exact:
            flags["exact"] = True
        if c.truncated:
            flags["truncated_by_source"] = True
        if c.uncertainty is not None:
            flags["standard_uncertainty"] = atlas_decimal(c.uncertainty)
        out.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": DATASET,
                "indicator": slug(c.name),
                "concept_id": None,
                "iso3": None,
                "provider_geo": NOT_GEOGRAPHIC,
                "entity_qid": None,
                "period_start": date(year, 1, 1),
                "period_end": date(year + 1, 1, 1),
                "value": atlas_decimal(c.value),
                "value_text": None,
                "unit": c.unit or "dimensionless",
                "flags": canon(flags),
                "raw_sha256": raw_sha256,
                "selector": canon({"file": "allascii.txt", "line": c.line}),
            }
        )
    return out


def kg_build(
    consts: Sequence[Constant], adj: str, year: int, *, raw_sha256: str, retrieved_at: datetime
) -> tuple[list[Any], list[Any]]:
    from reva.kg.model import KgEntity, KgStatement, KgTime

    ents: list[Any] = []
    stmts: list[Any] = []
    for c in consts:
        ents.append(
            KgEntity(
                qid=c.key,
                kind="physical_constant",
                labels={"en": c.name},
                descriptions={"en": f"CODATA recommended value ({adj}), NIST"},
                source_revision=f"codata:{year}",
            )
        )
        q: dict[str, Any] = {
            "unit": c.unit or "dimensionless",
            "adjustment": adj,
            "value_verbatim": c.value_text,
            "uncertainty_verbatim": c.uncertainty_text,
        }
        if c.exact:
            q["exact"] = True
        if c.truncated:
            q["truncated_by_source"] = True
        if c.uncertainty is not None:
            q["standard_uncertainty"] = atlas_decimal(c.uncertainty)
        stmts.append(
            KgStatement(
                subject_qid=c.key,
                property_pid="codata:value",
                value=quantity(c.value, c.unit or None),
                point_in_time=KgTime(f"{year:04d}", 9),
                qualifiers=q,
                references=(
                    {
                        "P854": URL,
                        "P813": retrieved_at.date().isoformat(),
                        "publisher": "NIST (CODATA)",
                        "selector": {"file": "allascii.txt", "line": c.line},
                        "licence_review": True,
                    },
                ),
                source_key=SOURCE_KEY,
                source_revision=f"codata:{year}",
                statement_id=f"{year}:{slug(c.name)}",
                raw_sha256=raw_sha256,
            )
        )
    return ents, stmts


__all__ = ["CONNECTOR_NAME", "Constant", "atlas_ingest", "atlas_source", "kg_build", "parse_listing", "rows"]
