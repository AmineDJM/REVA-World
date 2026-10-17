"""UNSD economic classifications -> KG industries (ISIC Rev.4) and products (CPC Ver.2.1) (V4, agent V4INDUSTRY).

Source
------
United Nations Statistics Division, classifications registry (``unstats.un.org/unsd/classifications/Econ``):

* ``ISIC_Rev_4_english_structure.Txt`` -- International Standard Industrial Classification of All Economic
  Activities, Rev.4: one row per code (``"Code","Description"``; 21 sections, 88 divisions, 238 groups, 419
  classes);
* ``CPC_Ver_2_1_english_structure.txt`` -- Central Product Classification Ver.2.1 (``"CPC21code","CPC21title"``,
  ISO-8859-1 text: 10 sections, 71 divisions, 329 groups, 1,299 classes, 2,887 subclasses);
* ``cpc21-isic4.txt`` -- UNSD correspondence CPC Ver.2.1 -> ISIC Rev.4 (``CPC21code, CPC21partial, ISIC4code,
  ISIC4partial``); its readme (archived with it) says: "The correspondence does not yet include divisions 61 and
  62 of the CPC. These will be added later." -- those CPC subclasses therefore have no origin statement (typed
  gap, reported), never an invented one.

Licence: UN Statistics Division publication; no dataset-specific licence statement on the download pages. Recorded
as ``un_terms_of_use_pending_review`` (international statistical standards published for free use; the UN
website terms restrict redistribution of *materials* -- maintainer review pending, flagged on every statement).

KG (source key ``unsd.classifications``; predicates in ``registry/kg/predicates_industry.yaml``)
----------------------------------------------------------------------------------------------
* entities ``isic4:<code>`` (kind ``industry``; label = UNSD description) and ``cpc21:<code>`` (kind ``product``);
  the code is the UNSD code verbatim (section letters for ISIC, digits for CPC);
* ``unsd:isic4_parent``: class -> group -> division (code prefixes) and division -> section (the section row that
  precedes the division in the structure file; ISIC division codes do not contain the section letter);
* ``unsd:cpc21_parent``: subclass -> class -> group -> division -> section (code prefixes);
* ``unsd:classification_level``;
* ``unsd:cpc21_isic4_origin``: one statement per correspondence row, qualifiers ``cpc21_partial`` /
  ``isic4_partial`` = UNSD's 0/1 flags (1: only part of the category corresponds).

Nothing is matched by name; a correspondence row whose code is absent from the structure files is refused
(``unknown_code``) and reported.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from reva.adapters.connectors.atlas_industry_common import code_digest, load_kg
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.unsd_classif"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-unsd-classif-1"
SOURCE_KEY = "unsd.classifications"
ATLAS_SOURCE = "unsd"
DATASET = "classifications"
HOST = "unstats.un.org"
BASE = f"https://{HOST}/unsd/classifications/Econ"
URLS: dict[str, str] = {
    "isic4": f"{BASE}/Download/In%20Text/ISIC_Rev_4_english_structure.Txt",
    "cpc21": f"{BASE}/Download/In%20Text/CPC_Ver_2_1_english_structure.txt",
    "cpc21_isic4": f"{BASE}/tables/CPC/CPCv21_ISIC4/cpc21-isic4.txt",
    "cpc21_isic4_readme": f"{BASE}/tables/CPC/CPCv21_ISIC4/readme.txt",
}
MAX_BYTES = 5_000_000
RIGHTS: dict[str, str] = {
    "licence": "UN Statistics Division classification publication (UN terms of use; pending maintainer review)",
    "licence_url": "https://www.un.org/en/about-us/terms-of-use",
    "licence_status": "un_terms_of_use_pending_review",
    "attribution": "United Nations Statistics Division, Classifications Registry (unstats.un.org/unsd/classifications)",
}
#: Minimum sizes of a complete structure file (UNSD counts above); fewer means a truncated or changed file.
MIN_ISIC = 760
MIN_CPC = 4500
MIN_CORR = 2500

ISIC_SECTION = re.compile(r"^[A-U]$")
ISIC_NUM = re.compile(r"^[0-9]{2,4}$")
CPC_CODE = re.compile(r"^[0-9]{1,5}$")
ISIC_LEVEL = {1: "section", 2: "division", 3: "group", 4: "class"}
CPC_LEVEL = {1: "section", 2: "division", 3: "group", 4: "class", 5: "subclass"}

P_ISIC_PARENT = "unsd:isic4_parent"
P_CPC_PARENT = "unsd:cpc21_parent"
P_ORIGIN = "unsd:cpc21_isic4_origin"
P_LEVEL = "unsd:classification_level"


class ClassifParseError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> ClassifParseError:
    return ClassifParseError(msg, retryable=False, kind=kind)


@dataclass(frozen=True)
class Code:
    scheme: str  # isic4 | cpc21
    code: str
    title: str
    parent: str | None
    level: str
    row: int  # 1-based data row in the file (header excluded)

    @property
    def key(self) -> str:
        return f"{self.scheme}:{self.code}"


@dataclass(frozen=True)
class Corr:
    cpc: str
    cpc_partial: int
    isic: str
    isic_partial: int
    row: int


# ============================================================================================== parsing
def _rows(body: bytes, header: Sequence[str], *, encoding: str) -> list[list[str]]:
    try:
        text = body.decode(encoding)
    except UnicodeDecodeError as e:
        raise _err(f"not {encoding} text ({e})") from None
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or [h.strip() for h in rows[0]] != list(header):
        raise _err(f"unexpected header {rows[0] if rows else None!r} (expected {list(header)})")
    return [r for r in rows[1:] if r and any(x.strip() for x in r)]


def parse_isic4(body: bytes, *, min_codes: int = MIN_ISIC) -> list[Code]:
    """ISIC Rev.4 structure rows -> codes with their parent (division -> preceding section row)."""
    out: list[Code] = []
    section: str | None = None
    seen: set[str] = set()
    for i, r in enumerate(_rows(body, ("Code", "Description"), encoding="utf-8"), start=1):
        if len(r) != 2:
            raise _err(f"isic4 row {i}: {len(r)} fields")
        code, title = r[0].strip(), r[1].strip()
        if not title:
            raise _err(f"isic4 row {i}: empty description")
        if code in seen:
            raise _err(f"isic4 row {i}: duplicate code {code!r}")
        seen.add(code)
        if ISIC_SECTION.match(code):
            section = code
            out.append(Code("isic4", code, title, None, "section", i))
            continue
        if not ISIC_NUM.match(code):
            raise _err(f"isic4 row {i}: unexpected code {code!r}")
        if len(code) == 2:
            if section is None:
                raise _err(f"isic4 row {i}: division {code} before any section")
            parent = section
        else:
            parent = code[:-1]
            if parent not in seen:
                raise _err(f"isic4 row {i}: parent {parent} of {code} not listed before it")
        out.append(Code("isic4", code, title, parent, ISIC_LEVEL[len(code)], i))
    if len(out) < min_codes:
        raise _err(f"isic4: only {len(out)} codes (expected >= {min_codes})", kind="incomplete")
    return out


def parse_cpc21(body: bytes, *, min_codes: int = MIN_CPC) -> list[Code]:
    """CPC Ver.2.1 structure rows (ISO-8859-1) -> codes; parent = code without its last digit."""
    out: list[Code] = []
    seen: set[str] = set()
    for i, r in enumerate(_rows(body, ("CPC21code", "CPC21title"), encoding="latin-1"), start=1):
        if len(r) != 2:
            raise _err(f"cpc21 row {i}: {len(r)} fields")
        code, title = r[0].strip(), r[1].strip()
        if not CPC_CODE.match(code) or not title:
            raise _err(f"cpc21 row {i}: unexpected code {code!r} / title")
        if code in seen:
            raise _err(f"cpc21 row {i}: duplicate code {code!r}")
        parent = code[:-1] or None
        if parent is not None and parent not in seen:
            raise _err(f"cpc21 row {i}: parent {parent} of {code} not listed before it")
        seen.add(code)
        out.append(Code("cpc21", code, title, parent, CPC_LEVEL[len(code)], i))
    if len(out) < min_codes:
        raise _err(f"cpc21: only {len(out)} codes (expected >= {min_codes})", kind="incomplete")
    return out


def parse_corr(body: bytes, *, min_rows: int = MIN_CORR) -> list[Corr]:
    out: list[Corr] = []
    hdr = ("CPC21code", "CPC21partial", "ISIC4code", "ISIC4partial")
    for i, r in enumerate(_rows(body, hdr, encoding="utf-8"), start=1):
        if len(r) != 4 or r[1].strip() not in ("0", "1") or r[3].strip() not in ("0", "1"):
            raise _err(f"cpc21-isic4 row {i}: {r!r}")
        out.append(Corr(r[0].strip(), int(r[1]), r[2].strip(), int(r[3]), i))
    if len(out) < min_rows:
        raise _err(f"cpc21-isic4: only {len(out)} rows (expected >= {min_rows})", kind="incomplete")
    return out


# ============================================================================================== KG
def kg_build(
    isic: Sequence[Code],
    cpc: Sequence[Code],
    corr: Sequence[Corr],
    *,
    raws: Mapping[str, str],
    retrieved_at: datetime,
    readme_note: str | None = None,
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """Entities + statements; ``raws`` maps ``isic4`` / ``cpc21`` / ``cpc21_isic4`` to the raw sha256."""
    from reva.kg.model import KgEntity, KgStatement

    files = {"isic4": "ISIC_Rev_4_english_structure.Txt", "cpc21": "CPC_Ver_2_1_english_structure.txt"}
    rev = {k: "sha256:" + v[:16] for k, v in raws.items()}
    ents: list[Any] = []
    sts: list[Any] = []
    for c in (*isic, *cpc):
        ents.append(
            KgEntity(
                qid=c.key,
                kind="industry" if c.scheme == "isic4" else "product",
                labels={"en": c.title[:500]},
                descriptions={
                    "en": (
                        f"ISIC Rev.4 {c.level} {c.code}"
                        if c.scheme == "isic4"
                        else f"CPC Ver.2.1 {c.level} {c.code}"
                    )
                },
                source_revision=rev[c.scheme],
            )
        )
        sel = {"file": files[c.scheme], "row": c.row, "code": c.code}
        ref = ({"stated_in": f"UNSD {files[c.scheme]}", "url": URLS[c.scheme], "selector": sel},)
        sts.append(
            KgStatement(
                subject_qid=c.key,
                property_pid=P_LEVEL,
                value={"type": "string", "value": c.level},
                references=ref,
                source_revision=rev[c.scheme],
                statement_id=f"{c.key}:level",
                raw_sha256=raws[c.scheme],
            )
        )
        if c.parent is not None:
            sts.append(
                KgStatement(
                    subject_qid=c.key,
                    property_pid=P_ISIC_PARENT if c.scheme == "isic4" else P_CPC_PARENT,
                    object_qid=f"{c.scheme}:{c.parent}",
                    qualifiers={
                        "basis": "preceding section row" if c.scheme == "isic4" and c.level == "division" else "code prefix"
                    },
                    references=ref,
                    source_revision=rev[c.scheme],
                    statement_id=f"{c.key}:parent",
                    raw_sha256=raws[c.scheme],
                )
            )
    isic_codes = {c.code for c in isic}
    cpc_codes = {c.code for c in cpc}
    skipped: Counter[str] = Counter()
    unknown: list[dict[str, Any]] = []
    for r in corr:
        if r.cpc not in cpc_codes or r.isic not in isic_codes:
            skipped["unknown_code"] += 1
            if len(unknown) < 20:
                unknown.append({"row": r.row, "cpc21": r.cpc, "isic4": r.isic})
            continue
        sts.append(
            KgStatement(
                subject_qid=f"cpc21:{r.cpc}",
                property_pid=P_ORIGIN,
                object_qid=f"isic4:{r.isic}",
                qualifiers={
                    "cpc21_partial": r.cpc_partial,
                    "isic4_partial": r.isic_partial,
                    "correspondence": "UNSD CPC Ver.2.1 - ISIC Rev.4",
                    **({"readme_caveat": readme_note} if readme_note else {}),
                },
                references=(
                    {
                        "stated_in": "UNSD cpc21-isic4.txt",
                        "url": URLS["cpc21_isic4"],
                        "selector": {"file": "cpc21-isic4.txt", "row": r.row},
                    },
                ),
                source_revision=rev["cpc21_isic4"],
                statement_id=f"cpc21_isic4:{r.cpc}:{r.isic}",
                raw_sha256=raws["cpc21_isic4"],
            )
        )
    covered = {r.cpc for r in corr}
    subclasses = {c.code for c in cpc if c.level == "subclass"}
    no_origin = sorted(subclasses - covered)
    info = {
        "isic4_codes": dict(Counter(c.level for c in isic)),
        "cpc21_codes": dict(Counter(c.level for c in cpc)),
        "correspondence_rows": len(corr),
        "correspondence_partial_flags": dict(Counter(f"{r.cpc_partial}{r.isic_partial}" for r in corr)),
        "cpc21_subclasses_without_origin": len(no_origin),
        "cpc21_subclasses_without_origin_divisions": dict(Counter(c[:2] for c in no_origin)),
        "skipped": dict(skipped),
        "unknown_codes_sample": unknown,
    }
    return ents, sts, info


# ============================================================================================== connector
def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="United Nations Statistics Division - Classifications Registry (ISIC Rev.4, CPC Ver.2.1)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']})",
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("ISIC Rev.4", "CPC Ver.2.1", "CPC Ver.2.1 - ISIC Rev.4 correspondence"),
        cadence_seconds=180 * 86400,
        planned_requests=len(URLS),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__]),
        field_mappings={"kg": sorted({P_ISIC_PARENT, P_CPC_PARENT, P_ORIGIN, P_LEVEL}), "urls": URLS},
        notes="KG only (no atlas table): isic4:<code> industries, cpc21:<code> products, hierarchy + correspondence.",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": DATASET}
    live = run.origin == "live"
    got: dict[str, Any] = {}
    for key, url in URLS.items():
        got[key] = await run.get(url, key=f"unsd:{key}", media_types=("text/plain", "text/csv"), max_bytes=MAX_BYTES)
    raws = {k: run.archive(v) for k, v in got.items()}
    isic = parse_isic4(got["isic4"].body, min_codes=MIN_ISIC if live else 1)
    cpc = parse_cpc21(got["cpc21"].body, min_codes=MIN_CPC if live else 1)
    corr = parse_corr(got["cpc21_isic4"].body, min_rows=MIN_CORR if live else 1)
    readme = got["cpc21_isic4_readme"].body.decode("utf-8", "replace")
    m = re.search(r"does not yet include[^\n]*", readme)
    note = m.group(0).strip() if m else None
    retrieved_at = max(v.retrieved_at for v in got.values())
    ents, sts, info = kg_build(isic, cpc, corr, raws=raws, retrieved_at=retrieved_at, readme_note=note)
    kg = load_kg(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=sorted(raws.values()),
        retrieved_at=retrieved_at,
        origin=run.origin,
        parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION, "rights": RIGHTS},
        predicate_prefix="unsd:",
        retract_prefix="",
    )
    return {"raw": raws, "build": info, "readme_caveat": note, "kg": kg}


__all__ = [
    "CONNECTOR_NAME",
    "URLS",
    "Code",
    "Corr",
    "atlas_ingest",
    "atlas_source",
    "kg_build",
    "parse_corr",
    "parse_cpc21",
    "parse_isic4",
]
