"""Comparative Constitutions Project (CCP) -> atlas tables (V3, agent A-LAW).

Sources (https://comparativeconstitutionsproject.org/download-data/, Box links, checked 2026-10-15):

* **Chronology of Constitutional Events, v6** (``ccpcce_v6.zip``, 0.8 MB; April 2026): one row per
  country-year, 1789-2025, of every independent state: constitutional system id, event id and
  event type (``new``, ``amendment``, ``interim``, ``reinstated``, ``suspension``, ``samendment``,
  ``non-event``). Atlas table ``ccp.chronology_v6``.
* **Characteristics of National Constitutions, v5** (``ccpcnc_v5.zip``, 118 MB; February 2025):
  expert coding of ~600 questions per country-year (1789-2023). Selected variables (constitutional
  provisions only — not statutes): ``systyear`` (year the constitutional system was promulgated),
  ``votemin`` (constitutional minimum voting age), ``cappun`` (constitutional treatment of capital
  punishment), ``samesexm``, ``marriage``, ``civmar``, each with CCP's article reference
  (``<var>_article``) and comments. Atlas table ``ccp.characteristics_v5``.

  **Blocked live (2026-10-15):** the ZIP is 118 MB, above ``ConnectorSpec.max_response_bytes``
  (≤ 50 MB, a reviewed bound of the port); the fetch therefore ends as a typed ``Blocked``
  (``response_cap``) until the lead raises the bound for atlas bulk connectors. The parser is
  implemented and tested on a subset fixture derived from the real file.

Rights: no explicit data licence; free download with a requested citation; the project's web terms
prohibit commercial use. Recorded in ``registry/sources/ccp.yaml`` and on every row.

Provenance:

* Chronology cells are verbatim. In v6 the 2020-2025 years list only *event* rows for most
  countries (non-event years after 2019 are absent, checked against e.g. France 2019 -> 2024): a
  missing 2020-2025 row is *not reported*, not "no event" (coverage note of the table).
* Characteristics codes are mapped to their codebook labels by :data:`CODEBOOK` — transcribed by
  the agent from ``codebook_v5.pdf`` (sha256 87e29569…, printed page numbers listed per variable),
  maintainer review pending — and keep ``flags.provider_code``. ``votemin`` codes 1/2/3 are the
  ages 18/20/21 (numeric ``value``); other codes are ``value_text`` labels (e.g. "adult/majority/
  age of majority", "left explicitly to non-constitutional law", "Not Specified").
* Geography: ``provider_geo`` = ``"<cowcode>|<country>"`` verbatim (CCP uses one COW code for several
  names, e.g. 340 Serbia / Yugoslavia / Serbia and Montenegro); mapped by the agent's ``ccp``
  scheme. Names CCP uses for a predecessor+successor series (Russia (Soviet Union), Turkey/Ottoman
  Empire, …) map to the successor and every row says so in ``flags.provider_continuity``.
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from collections import defaultdict
from collections.abc import Iterator, Sequence
from typing import Any

from reva.adapters.connectors import atlas_law_common as common
from reva.atlas.tables import year_period
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

SOURCE_KEY = "ccp.data"
ATLAS_SOURCE = "ccp"
CONNECTOR_NAME = "atlas.ccp"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-ccp-parse-1"
BOX = "https://utexas.box.com/shared/static"
HOSTS = ("utexas.box.com", "public.boxcloud.com")
ZIP_MEDIA = ("application/zip", "application/octet-stream", "application/x-zip-compressed")
MAX_SPEC_BYTES = 50_000_000  # ConnectorSpec bound (le=52_428_800)

ITEMS: dict[str, dict[str, Any]] = {
    "chronology": {
        "url": f"{BOX}/qzs6irrpbfzspvv6sq94r98yw9rsh5lz.zip",
        "member": "ccpcce_v6/ccpcce/ccpcce_v6.csv",
        "dataset": "chronology_v6",
        "max_bytes": 5_000_000,
        "max_member_bytes": 20_000_000,
    },
    "characteristics": {
        "url": f"{BOX}/cpgysqaogi1590ucv5x0wn59urqm00x3.zip",
        "member": "ccpcnc_v5/ccpcnc/ccpcnc_v5.csv",
        "dataset": "characteristics_v5",
        "max_bytes": 130_000_000,  # real size 118,231,611 B (2026-10-15); exceeds the spec bound
        "max_member_bytes": 300_000_000,
    },
}
RIGHTS = {
    "licence": "No explicit data licence; free download with requested citation; CCP web terms prohibit commercial use",
    "licence_url": "https://www.constituteproject.org/content/terms",
    "attribution": "Comparative Constitutions Project (Elkins, Ginsburg, Melton)",
    "commercial_use": "not_permitted_without_permission",
}
CODEBOOK_DOC = "CCP Characteristics of National Constitutions v5 codebook_v5.pdf (sha256 87e29569cad08eb1…)"
CHRONOLOGY_EVENTS = frozenset({"new", "amendment", "interim", "reinstated", "suspension", "samendment", "non-event"})
CHRONOLOGY_COLUMNS = ("cowcode", "country", "year", "systid", "evntid", "evnttype")
CHRONOLOGY_LAST_FULL_YEAR = 2019

#: CCP names that span a predecessor and its successor under one COW code (see geo_aliases/a_law.yaml).
PROVIDER_CONTINUITY: dict[str, str] = {
    # Chronology v6 names
    "325|Italy/Sardinia": "Kingdom of Sardinia -> Italy",
    "365|Russia (Soviet Union)": "Russian Empire / Soviet Union / Russian Federation",
    "510|Tanzania/Tanganyika": "Tanganyika -> United Republic of Tanzania (1964)",
    "552|Zimbabwe (Rhodesia)": "Rhodesia (UDI 1965) -> Zimbabwe (1980)",
    "640|Turkey/Ottoman Empire": "Ottoman Empire -> Republic of Turkey (1923)",
    "678|Yemen (Arab Republic of Yemen)": "Yemen Arab Republic (North Yemen) -> Republic of Yemen (1990)",
    "816|Vietnam, Democratic Republic of": "Democratic Republic of Vietnam -> Socialist Republic of Viet Nam (1976)",
    # Characteristics v5 names of the same series
    "325|Italy": "Kingdom of Sardinia -> Italy",
    "365|Russia": "Russian Empire / Soviet Union / Russian Federation",
    "510|Tanzania": "Tanganyika -> United Republic of Tanzania (1964)",
    "552|Zimbabwe": "Rhodesia (UDI 1965) -> Zimbabwe (1980)",
    "640|Turkey": "Ottoman Empire -> Republic of Turkey (1923)",
    "678|Yemen Arab Republic": "Yemen Arab Republic (North Yemen) -> Republic of Yemen (1990)",
    "816|Vietnam": "Democratic Republic of Vietnam -> Socialist Republic of Viet Nam (1976)",
}

#: Selected characteristics variables: code -> (label, numeric value or None), with codebook locator.
CODEBOOK: dict[str, dict[str, Any]] = {
    "systyear": {
        "variable": "SYSTYEAR",
        "page": 11,
        "pdf_page": 12,
        "unit": "year",
        "numeric": True,
        "question": "Year in which the constitutional system was promulgated.",
    },
    "votemin": {
        "variable": "v427 VOTEMIN",
        "page": 103,
        "pdf_page": 104,
        "unit": "years (age)",
        "question": "What is the minimum age limit for voting? (Asked only if VOTERES is answered 1)",
        "codes": {
            "1": ("18", "18"),
            "2": ("20", "20"),
            "3": ("21", "21"),
            "4": ("adult/majority/age of majority", None),
            "90": ("left explicitly to non-constitutional law", None),
            "96": ("other, please specify in the comments section", None),
            "97": ("Unable to Determine", None),
            "98": ("Not Specified", None),
            "99": ("Not Applicable", None),
        },
    },
    "cappun": {
        "variable": "v519 CAPPUN",
        "page": 122,
        "pdf_page": 123,
        "unit": "category",
        "question": "How does the constitution treat the use of capital punishment?",
        "codes": {
            "1": ("Universally Prohibited", None),
            "2": ("Prohibited Except in the Case of War", None),
            "3": ("Explicitly Allowed", None),
            "90": ("left explicitly to non-constitutional law", None),
            "96": ("other, please specify in the comments section", None),
            "97": ("Unable to Determine", None),
            "98": ("Not Specified", None),
        },
    },
    "marriage": {
        "variable": "v594 MARRIAGE",
        "page": 140,
        "pdf_page": 141,
        "unit": "category",
        "question": "Does the constitution provide for the right to marry?",
        "codes": {
            "1": ("Yes, general provision", None),
            "2": ("Yes, marriage allowed between a man and woman", None),
            "3": ("No", None),
            "90": ("left explicitly to non-constitutional law", None),
            "96": ("other, please specify in the comments section", None),
            "97": ("Unable to Determine", None),
        },
    },
    "samesexm": {
        "variable": "v595 SAMESEXM",
        "page": 140,
        "pdf_page": 141,
        "unit": "category",
        "question": "Does the constitution provide the right for same sex marriages?",
        "codes": {
            "1": ("Yes", None),
            "2": ("No", None),
            "96": ("other, please specify in the comments section", None),
            "97": ("Unable to Determine", None),
        },
    },
    "civmar": {
        "variable": "v600 CIVMAR",
        "page": 141,
        "pdf_page": 142,
        "unit": "category",
        "question": "Is there a constitutional provision for civil marriage?",
        "codes": {
            "1": ("Yes", None),
            "2": ("No", None),
            "96": ("other, please specify in the comments section", None),
            "97": ("Unable to Determine", None),
        },
    },
}
CHAR_ID_COLUMNS = (
    "cowcode",
    "country",
    "year",
    "systid",
    "systyear",
    "evntid",
    "c_inforce",
    "coding_available",
    "coding_imputed",
)


class CcpParseError(SourceResponseError):
    pass


def _err(msg: str) -> CcpParseError:
    return CcpParseError(msg, retryable=False, kind="schema_drift")


class CcpConnector:
    def describe(self) -> ConnectorSpec:
        return common.law_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="Comparative Constitutions Project",
            licence=RIGHTS["licence"],
            allowed_hosts=HOSTS,
            dataset_ids=tuple(v["dataset"] for v in ITEMS.values()),
            field_mappings={
                "tables": {k: f"{ATLAS_SOURCE}.{v['dataset']}" for k, v in ITEMS.items()},
                "codebook": {k: {kk: vv for kk, vv in v.items() if kk != "codes"} for k, v in CODEBOOK.items()},
            },
            units={k: v["unit"] for k, v in CODEBOOK.items()},
            geography=("ALL_CCP_STATES",),
            time_semantics="country-year (event year / content in force at end of year)",
            lineage_family="ccp",
            cadence_seconds=180 * 86400,
            revision_strategy="versioned releases (new atlas table version per release)",
            max_response_bytes=MAX_SPEC_BYTES,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=common.code_digest(sys.modules[__name__]),
        )

    def items(self, keys: Sequence[str] | None = None) -> tuple[FetchItem, ...]:
        return tuple(
            FetchItem(key=k, url=v["url"], expected_media_types=ZIP_MEDIA, max_bytes=v["max_bytes"])
            for k, v in ITEMS.items()
            if keys is None or k in keys
        )


def read_member(body: bytes, member: str, max_bytes: int) -> bytes:
    """One named CSV member of a provider ZIP (bounded; nothing extracted to disk)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err("download is not a ZIP archive") from e
    try:
        info = z.getinfo(member)
    except KeyError as e:
        raise _err(f"ZIP lacks member {member!r}") from e
    if info.file_size > max_bytes:
        raise _err(f"member {member} expands to {info.file_size} bytes (> {max_bytes})")
    with z.open(info) as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise _err(f"member {member} exceeds its bound while reading")
    return data


def _csv_rows(data: bytes) -> Iterator[tuple[int, dict[str, str]]]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    yield from enumerate(reader, start=2)  # line 1 = header


def _geo(cow: str, country: str) -> str:
    g = f"{cow}|{country}"
    if len(g) > 64:
        raise _err(f"provider geography too long: {g!r}")
    return g


def _base(geo: str, doc: str) -> dict[str, Any]:
    f: dict[str, Any] = {"licence": RIGHTS["licence"], "commercial_use": RIGHTS["commercial_use"], "release": doc}
    if geo in PROVIDER_CONTINUITY:
        f["provider_continuity"] = (
            f"CCP codes {PROVIDER_CONTINUITY[geo]} as one country; rows before the successor existed concern the "
            "predecessor state"
        )
    return f


def parse_chronology(body: bytes, raw_sha256: str) -> list[dict[str, Any]]:
    item = ITEMS["chronology"]
    data = read_member(body, item["member"], item["max_member_bytes"])
    rows: list[dict[str, Any]] = []
    seq: dict[tuple[str, int], int] = defaultdict(int)
    header_checked = False
    for line, r in _csv_rows(data):
        if not header_checked:
            if tuple(r.keys()) != CHRONOLOGY_COLUMNS:
                raise _err(f"chronology columns {tuple(r.keys())} != {CHRONOLOGY_COLUMNS}")
            header_checked = True
        cow, country, year = r["cowcode"].strip(), r["country"].strip(), r["year"].strip()
        if not cow.isdigit() or not year.isdigit() or not country:
            raise _err(f"line {line}: bad cowcode/year/country")
        ev = r["evnttype"].strip()
        if ev not in CHRONOLOGY_EVENTS:
            raise _err(f"line {line}: unknown event type {ev!r}")
        geo = _geo(cow, country)
        y = int(year)
        seq[(geo, y)] += 1
        n = seq[(geo, y)]
        suffix = "" if n == 1 else f"|event={n}"
        flags = {**_base(geo, "Chronology of Constitutional Events v6 (2026-04-06)"), "transcription": common.VERBATIM}
        if n > 1:
            flags["same_year_event_order"] = n
        sel = {"file": item["member"], "line": line}
        period = year_period(y)
        rows.append(
            common.row(
                source=ATLAS_SOURCE,
                dataset=item["dataset"],
                indicator=f"evnttype{suffix}",
                provider_geo=geo,
                period=period,
                raw_sha256=raw_sha256,
                selector={**sel, "column": "evnttype"},
                flags=flags,
                unit="category",
                value_text=ev,
            )
        )
        for col in ("systid", "evntid"):
            v = r[col].strip()
            if v:
                if not v.isdigit():
                    raise _err(f"line {line}: {col} is not an integer ({v!r})")
                rows.append(
                    common.row(
                        source=ATLAS_SOURCE,
                        dataset=item["dataset"],
                        indicator=f"{col}{suffix}",
                        provider_geo=geo,
                        period=period,
                        raw_sha256=raw_sha256,
                        selector={**sel, "column": col},
                        flags=flags,
                        unit="id",
                        value=v,
                    )
                )
    if not rows:
        raise _err("chronology is empty")
    return rows


def parse_characteristics(
    body: bytes, raw_sha256: str, variables: Sequence[str] = tuple(CODEBOOK)
) -> list[dict[str, Any]]:
    item = ITEMS["characteristics"]
    data = read_member(body, item["member"], item["max_member_bytes"])
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    header_checked = False
    for line, r in _csv_rows(data):
        if not header_checked:
            need = [*CHAR_ID_COLUMNS, *variables] + [f"{v}_article" for v in variables if v != "systyear"]
            miss = [c for c in need if c not in r]
            if miss:
                raise _err(f"characteristics CSV lacks columns {miss[:8]}")
            header_checked = True
        cow, country, year = r["cowcode"].strip(), r["country"].strip(), r["year"].strip()
        if not cow.isdigit() or not year.isdigit():
            raise _err(f"line {line}: bad cowcode/year")
        geo = _geo(cow, country)
        # CCP lists a second observation for a country-year with two systems (MULTIPLE = 1, e.g. 340
        # Serbia 2006: systems 461 and 462); later observations get the system id folded in.
        suffix = "" if (geo, year) not in seen else f"|systid={r['systid'].strip()}"
        seen.add((geo, year))
        base = {
            **_base(geo, "Characteristics of National Constitutions v5 (2025-02)"),
            "systid": r["systid"].strip() or None,
            "c_inforce": r["c_inforce"].strip() or None,
            "coding_available": r["coding_available"].strip() or None,
            "coding_imputed": r["coding_imputed"].strip() or None,
            "content_as_of": "constitution as in force at the end of the year (CCP sample rule)",
        }
        period = year_period(int(year))
        for var in variables:
            code = (r.get(var) or "").strip()
            if not code:
                continue
            cb = CODEBOOK[var]
            locator = {
                "codebook": "codebook_v5.pdf",
                "variable": cb["variable"],
                "page": cb["page"],
                "pdf_page": cb["pdf_page"],
            }
            flags = {**base, "codebook": locator, "question": cb["question"]}
            value: str | None
            text: str | None
            if cb.get("numeric"):
                if not code.isdigit():
                    raise _err(f"line {line}: {var} is not a year ({code!r})")
                value, text = code, None
                flags["transcription"] = common.VERBATIM
            else:
                if code not in cb["codes"]:
                    raise _err(f"line {line}: {var} code {code!r} is not in the codebook table")
                label, num = cb["codes"][code]
                value, text = (num, None) if num is not None else (None, label)
                flags["provider_code"] = code
                flags["label"] = label
                flags["transcription"] = common.transcribed(CODEBOOK_DOC)
            art = (r.get(f"{var}_article") or "").strip()
            if art:
                flags["constitution_article"] = common.clip_text(art, 300)[0]
            com = (r.get(f"{var}_comments") or "").strip()
            if com:
                t, cut = common.clip_text(com, 1200)
                flags["ccp_comments"] = t
                if cut:
                    flags["ccp_comments_truncated"] = True
            rows.append(
                common.row(
                    source=ATLAS_SOURCE,
                    dataset=item["dataset"],
                    indicator=var + suffix,
                    provider_geo=geo,
                    period=period,
                    raw_sha256=raw_sha256,
                    selector={"file": item["member"], "line": line, "column": var},
                    flags=flags,
                    unit=cb["unit"],
                    value=value,
                    value_text=text,
                )
            )
    if not rows:
        raise _err("characteristics yielded no rows")
    return rows


CHRONOLOGY_NOTE = (
    "CCP Chronology v6: every country-year 1789-2019 of every independent state; for 2020-2025 only event "
    "rows are listed for most countries (a missing 2020-2025 row is not reported, not 'no event')."
)
CHARACTERISTICS_NOTE = (
    "CCP Characteristics v5: constitutional provisions (not statutes) 1789-2023, selected variables "
    "systyear, votemin, cappun, marriage, samesexm, civmar; codes mapped to codebook labels by the agent."
)


async def run(
    engine: Any,
    store: Any,
    http: Any,
    *,
    origin: str,
    keys: Sequence[str] | None = None,
    actor: str = "worker:atlas_ccp",
) -> common.IngestReport:
    from reva.atlas.tables import register_atlas_table

    conn = CcpConnector()
    spec = conn.describe()
    report = common.IngestReport(CONNECTOR_NAME)
    for item in conn.items(keys):
        if item.max_bytes > spec.max_response_bytes:
            # Refused before any byte is requested: the archive is larger than the connector's
            # reviewed response bound (the live client enforces the same bound).
            report.blocked.append(
                common.Blocked(
                    SOURCE_KEY,
                    item.key,
                    "response_cap",
                    f"provider file ~{ITEMS[item.key]['max_bytes'] // 1_000_000} MB > "
                    f"ConnectorSpec.max_response_bytes {spec.max_response_bytes}; needs a lead-approved bound",
                )
            )
            continue
        got: RawAcquisition | common.Blocked = await common.fetch_raw(http, item, SOURCE_KEY)
        if isinstance(got, common.Blocked):
            report.blocked.append(got)
            continue
        sha = common.archive_raw(engine, store, spec, got, actor=actor)
        report.raw.append({"item": item.key, "raw_sha256": sha, "bytes": len(got.body), "url": item.url})
        if item.key == "chronology":
            rows, note = parse_chronology(got.body, sha), CHRONOLOGY_NOTE
        else:
            rows, note = parse_characteristics(got.body, sha), CHARACTERISTICS_NOTE
        s = register_atlas_table(
            engine,
            store,
            source=ATLAS_SOURCE,
            dataset=ITEMS[item.key]["dataset"],
            frame=common.atlas_frame(rows),
            raw_sha256s=[sha],
            parser_version=PARSER_VERSION,
            origin=origin,
            geo_scheme="ccp",
            rights=RIGHTS,
            coverage_note=note,
        )
        report.tables.append(
            {k: s[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created")}
            | {"countries": s["coverage"]["countries"], "unmapped_provider_geo_count": len(s["unmapped_provider_geo"])}
        )
    return report


__all__ = [
    "ATLAS_SOURCE",
    "CODEBOOK",
    "CONNECTOR_NAME",
    "PROVIDER_CONTINUITY",
    "CcpConnector",
    "parse_characteristics",
    "parse_chronology",
    "read_member",
    "run",
]
