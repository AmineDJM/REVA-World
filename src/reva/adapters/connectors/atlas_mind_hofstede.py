"""Hofstede cultural dimensions (country scores) -> atlas table ``hofstede.dimensions_2015`` (V4, agent V4MIND).

Source: Geert Hofstede's "Dimension data matrix" page (https://geerthofstede.com/research-and-vsm/dimension-data-matrix/),
file ``6-dimensions-for-website-2015-08-16.csv`` ("the base culture data for six dimensions of culture as presented
in Cultures and Organizations 3rd edition 2010 ... These are the data as used in our books (version 2015 12 08)").
Semicolon-separated: ``ctr;country;pdi;idv;mas;uai;ltowvs;ivr`` -- power distance, individualism, masculinity,
uncertainty avoidance, long-term orientation (WVS-based), indulgence versus restraint; ``#NULL!`` = no score.

Licence (page text, verbatim, read 2026-10-17): "Researchers can use them without asking for permission. Those who
are considering commercial use should contact us." -- research use only; no open licence, no redistribution
grant. Every cell carries ``flags.licence_restriction`` and the table rights say so; listed as an owner decision
in ``docs/dev/v4_v4mind.md`` (keep for research only, or drop). The licence page is fetched and archived on every
run, and a change of that sentence refuses registration.

Geography: the ``ctr`` column is Hofstede's own 3-letter code, **not** ISO 3166 (``CHI`` = China, ``BEN`` = Belgium
Netherl, ``CAF`` = Canada French, ``MAC`` = Macedonia Rep, ...), so codes are mapped through scheme ``hofstede``
(``registry/atlas/geo_aliases/v4mind.yaml``) built from Hofstede's *country name* column (exact crosswalk label or a
listed name with its evidence). Regions ("Arab countries", "Africa East/West") and sub-national groups ("Belgium
French", "Canada French", "South Africa white", "Switzerland French/German", "Germany East") stay unmapped.

Period: the file has no measurement year. Scores rest on the IBM surveys (around 1967-1973) and later replications;
the cell period is the edition year of the book the page names (2010), with ``flags.period_basis`` saying so. These
are scores of national cultures on a 0-100-ish scale (some replication values fall outside 0-100, as the page
notes); they describe averages of samples, not individuals, and imply nothing causal.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from reva.adapters.connectors.atlas_society_common import (
    RowBuffer,
    canon,
    concept_hints,
    err,
    fetch_archive,
    num,
    year_period,
)
from reva.adapters.connectors.base import module_digest
from reva.atlas.sources import AtlasSourceSpec
from reva.ports.connectors import FetchItem

CONNECTOR_NAME = "atlas.hofstede"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_mind_hofstede/1"
SOURCE = "hofstede"
DATASET = "dimensions_2015"
SOURCE_KEY = "hofstede.dimensions"
HOST = "geerthofstede.com"
PAGE_URL = "https://geerthofstede.com/research-and-vsm/dimension-data-matrix/"
CSV_URL = "https://geerthofstede.com/wp-content/uploads/2016/08/6-dimensions-for-website-2015-08-16.csv"
GEO_SCHEME = "hofstede"
CATALOG_FILE = "mind.yaml"
PERIOD_YEAR = 2010
LICENCE_SENTENCE = "Researchers can use them without asking for permission."
LICENCE_SENTENCE_2 = "Those who are considering commercial use should contact us."
VERSION_SENTENCE = "These are the data as used in our books (version 2015 12 08)"
#: column -> dimension name (Hofstede's own names, Cultures and Organizations 3rd ed.)
DIMENSIONS: dict[str, str] = {
    "pdi": "Power distance index",
    "idv": "Individualism versus collectivism",
    "mas": "Masculinity versus femininity",
    "uai": "Uncertainty avoidance index",
    "ltowvs": "Long-term versus short-term orientation (WVS-based)",
    "ivr": "Indulgence versus restraint",
}
NULL_CODE = "#NULL!"
RIGHTS: dict[str, Any] = {
    "licence": "research use without permission; commercial use requires contacting the authors (no open licence)",
    "licence_url": PAGE_URL,
    "licence_restriction": "research_only_no_redistribution_grant",
    "attribution": "Hofstede, G., Hofstede, G.J. & Minkov, M. (2010). Cultures and Organizations: Software of the "
    "Mind, 3rd ed.; data: geerthofstede.com dimension data matrix (version 2015 12 08)",
    "source_key": SOURCE_KEY,
    "agent": "V4MIND",
}


def items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="hofstede.page", url=PAGE_URL, max_bytes=2_000_000, expected_media_types=("text/html",)),
        FetchItem(key="hofstede.csv", url=CSV_URL, max_bytes=1_000_000, expected_media_types=("text/csv",)),
    )


def check_page(body: bytes) -> dict[str, str]:
    """The licence and version sentences the cells rely on must still be on the page."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body.decode("utf-8", errors="replace")))
    for s in (LICENCE_SENTENCE, LICENCE_SENTENCE_2, VERSION_SENTENCE):
        if s not in text:
            raise err(f"Hofstede page: sentence {s!r} not found; review the licence/version", kind="licence")
    return {"licence_text": f"{LICENCE_SENTENCE} {LICENCE_SENTENCE_2}", "version_text": VERSION_SENTENCE}


def parse(body: bytes, *, raw_sha256: str) -> tuple[Any, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise err(f"Hofstede CSV is not UTF-8 ({e})") from e
    rows = list(csv.reader(io.StringIO(text.removeprefix("﻿")), delimiter=";"))
    header = [h.strip() for h in rows[0]] if rows else []
    if header != ["ctr", "country", *DIMENSIONS]:
        raise err(f"Hofstede CSV header changed: {header}")
    hints = concept_hints(CATALOG_FILE, SOURCE, DATASET)
    buf = RowBuffer(SOURCE, DATASET)
    seen: set[str] = set()
    unmapped: dict[str, str] = {}
    nulls = 0
    for line, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        if len(r) != len(header):
            raise err(f"Hofstede CSV line {line}: {len(r)} fields")
        code, name = r[0].strip(), r[1].strip()
        if not code or code in seen:
            raise err(f"Hofstede CSV line {line}: empty or repeated code {code!r}")
        seen.add(code)
        iso3 = country_iso3(code, GEO_SCHEME)
        if iso3 is None:
            unmapped[code] = name
        for j, (col, dim) in enumerate(DIMENSIONS.items(), start=2):
            cell = r[j].strip()
            flags: dict[str, Any] = {
                "country_name": name,
                "dimension": dim,
                "period_basis": "edition year of Cultures and Organizations 3rd ed. (2010) named by the source; "
                "scores rest on IBM surveys (~1967-1973) and later replications, not a measurement year",
                "data_version": "2015 12 08",
                "licence_restriction": RIGHTS["licence_restriction"],
            }
            value = None
            if cell == NULL_CODE or cell == "":
                flags["missing"] = "provider_missing_code"
                flags["provider_flag"] = cell or "(empty)"
                nulls += 1
            else:
                value = num(cell)
                if value is None:
                    raise err(f"Hofstede CSV line {line} {col}: {cell!r} is not a number")
            buf.add(
                indicator=col,
                provider_geo=code,
                period=year_period(PERIOD_YEAR),
                value=value,
                unit="index score (Hofstede scale, nominally 0-100)",
                flags=canon(flags),
                raw_sha256=raw_sha256,
                selector=canon(file=CSV_URL.rsplit("/", 1)[-1], line=line, column=col),
                iso3=iso3,
                concept_id=hints.get(col),
            )
    if len(seen) < 100:
        raise err(f"Hofstede CSV: only {len(seen)} rows (expected ~111): truncated?")
    return buf.frame(), {"units": len(seen), "cells": len(buf), "null_cells": nulls, "unmapped": unmapped}


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="Geert Hofstede (dimension data matrix, geerthofstede.com)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=(CSV_URL.rsplit("/", 1)[-1],),
        cadence_seconds=365 * 86400,
        planned_requests=len(items()),
        max_response_bytes=2_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=module_digest(parse),
        field_mappings={"geo_scheme": GEO_SCHEME, "dimensions": DIMENSIONS, "period_year": PERIOD_YEAR},
        notes="Licence page + one CSV (4 kB); research-use-only data, flagged on every cell.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    got = await fetch_archive(run, items())
    lic = check_page(got.bodies["hofstede.page"])
    frame, rep = parse(got.bodies["hofstede.csv"], raw_sha256=got.shas["hofstede.csv"])
    note = (
        "Hofstede six cultural dimensions (pdi, idv, mas, uai, ltowvs, ivr), dimension data matrix version 2015 12 08; "
        "period = 2010 edition year (no measurement year in the file); #NULL! kept as typed missing; Hofstede codes "
        "mapped by country name (scheme hofstede); regions and sub-national groups unmapped. Research use only "
        "(page: '" + lic["licence_text"] + "')."
    )
    reg = run.register(
        dataset=DATASET, frame=frame, raw_sha256s=[got.shas["hofstede.csv"]], coverage_note=note,
        geo_scheme=GEO_SCHEME, rights={**RIGHTS, **lic, "licence_page_raw_sha256": got.shas["hofstede.page"]},
    )  # fmt: skip
    return {"parse": rep, "licence": lic, "atlas": reg}


__all__ = ["CONNECTOR_NAME", "DIMENSIONS", "atlas_ingest", "atlas_source", "check_page", "items", "parse"]
