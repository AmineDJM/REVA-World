"""Ratification status of the 18 core international human rights instruments (OHCHR dashboard; V3, A-TREATIES).

Source
------
OHCHR's "Status of Ratification Interactive Dashboard" (https://indicators.ohchr.org) is served by a small JSON
web API that the dashboard's own script calls (``/webapi/dashboarddata/<Method>``; GET, no key):

* ``GetRatifications`` — the 18 instruments (``Id``, ``Acronym``, ``RatificationName``, ``LastSynced``);
* ``GetCountries`` — 199 areas (``Id``, ``AreaId`` = ISO 3166-1 alpha-3 except ``PLS`` Palestine and ``ERU``
  European Union, ``CountryName``);
* ``GetCountriesForRatification?ratificationId=<Id>`` — per State: ``CountryStatus`` (dashboard enum
  ``NoAction 0, Signatory 1, StateParty 2``), ``SignatoryDate``, ``StatePartyDate``, ``StatePartyType``,
  ``DenunciationDate`` (``dd/mm/yyyy``; ``01/01/1753`` = no date), the text of reservations and declarations
  (HTML), and whether the State accepts the individual complaints / inquiry procedure (with date).

``StatePartyType`` is not documented by OHCHR. Its meaning was established by comparing every ICCPR record with
the UN Treaty Collection status page (MTDSG IV-4, archived 2026-10-16): ``0`` = accession (99 of 99 rows coded
``a`` by UNTC), ``1`` = succession (8 of 8 ``d``), ``2`` = ratification (67 of 67 uncoded), ``3`` = no consent
instrument (7 of 7 without a UNTC consent date); all 175 consent dates agree. ``tests/unit/test_atlas_treaties.py``
re-checks the mapping on the fixture slices. Other codes are kept verbatim (``code:<n>``).

OHCHR states that the data come from the UN Office of Legal Affairs (the depositary; see ``atlas_treaties``) and
are updated "every six months or after a new ratification" (metadata sheet); the per-treaty ``LastSynced`` time
is the as-of date of each status row. Rights: OHCHR is part of the UN Secretariat; the UN Terms of Use apply
(personal, non-commercial use; no redistribution or derivative compilations without permission) — **not an
open licence** (OHCHR's own copyright page is behind a bot challenge and was not readable).

Output: atlas table ``ohchr.hr_ratification`` (no KG statements: the depositary record is loaded into the KG by
``atlas.untc``; this table adds OHCHR's reservations/declarations texts and complaint-procedure acceptance).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.ohchr"
VERSION = "1.0.0"
SOURCE_KEY = "ohchr.indicators"
ATLAS_SOURCE = "ohchr"
DATASET = "hr_ratification"
PARSER_VERSION = "atlas_ohchr/1"
GEO_SCHEME = "ohchr"
HOST = "indicators.ohchr.org"
BASE = f"https://{HOST}/webapi/dashboarddata/"
URL_TREATIES = BASE + "GetRatifications"
URL_COUNTRIES = BASE + "GetCountries"
LICENCE = (
    "UN Terms of Use (OHCHR is part of the UN Secretariat): personal, non-commercial use; no redistribution or "
    "derivative compilations without permission -- not an open licence"
)
NO_DATE = "01/01/1753"
MAX_TEXT = 4000
EXPECTED_TREATIES = 18
#: dashboard script enum ``CountryStatus={NoAction:0,Signatory:1,StateParty:2}`` (bundles/dashboard, 2026-10-16)
COUNTRY_STATUS = {0: "no_action", 1: "signatory", 2: "state_party"}
#: established against UNTC MTDSG IV-4 (see module doc)
STATE_PARTY_TYPE = {0: "accession", 1: "succession", 2: "ratification", 3: "none"}
#: OHCHR acronym -> UNTC MTDSG number (same instrument; titles checked in the tests)
ACRONYM_MTDSG = {
    "ICERD": "IV-2", "ICESCR": "IV-3", "CESCR-OP": "IV-3-a", "ICCPR": "IV-4", "ICCPR-OP1": "IV-5",
    "ICCPR-OP2": "IV-12", "CEDAW": "IV-8", "OP-CEDAW": "IV-8-b", "CAT": "IV-9", "OP-CAT": "IV-9-b", "CRC": "IV-11",
    "OP-CRC-AC": "IV-11-b", "OP-CRC-SC": "IV-11-c", "CRC-OP-IC": "IV-11-d", "ICRMW": "IV-13", "CRPD": "IV-15",
    "CRPD-OP": "IV-15-a", "CPED": "IV-16",
}  # fmt: skip


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Office of the United Nations High Commissioner for Human Rights (OHCHR), Status of Ratification "
        "Interactive Dashboard",
        licence=LICENCE,
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=tuple(ACRONYM_MTDSG),
        cadence_seconds=30 * 86400,
        planned_requests=EXPECTED_TREATIES + 4,
        max_response_bytes=20_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        notes="dashboard JSON web API: 2 list calls + 1 call per instrument",
    )


class OhchrParseError(ValueError):
    pass


def text_of(fragment: str | None) -> str:
    if not fragment:
        return ""
    t = html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()  # one line: atlas text cells refuse control characters


def ddmmyyyy(s: str | None) -> date | None:
    if not s or s == NO_DATE:
        return None
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s.strip())
    if not m:
        raise OhchrParseError(f"unexpected date {s!r}")
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))


def ratification_url(rid: int) -> str:
    return f"{BASE}GetCountriesForRatification?ratificationId={int(rid)}"


def _json(body: bytes, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise OhchrParseError(f"{what}: not JSON ({e})") from e


def parse_treaties(body: bytes) -> list[dict[str, Any]]:
    doc = _json(body, "GetRatifications")
    if not isinstance(doc, list):
        raise OhchrParseError("GetRatifications: not a list")
    out = []
    for r in doc:
        if not {"Id", "Acronym", "RatificationName", "LastSynced"} <= set(r):
            raise OhchrParseError(f"GetRatifications: missing keys in {sorted(r)[:8]}")
        out.append(r)
    return out


def parse_countries(body: bytes) -> dict[int, dict[str, Any]]:
    doc = _json(body, "GetCountries")
    if not isinstance(doc, list) or not all({"Id", "AreaId", "CountryName"} <= set(r) for r in doc):
        raise OhchrParseError("GetCountries: unexpected shape")
    return {int(r["Id"]): r for r in doc}


REQUIRED = {"CountryId", "RatificationId", "CountryStatus", "SignatoryDate", "StatePartyDate", "StatePartyType",
            "DenunciationDate", "Reservation", "Declaration", "IsIndividualComAllowed", "IndividualComDate",
            "IsInquiryProcAllowed", "InquiryProcDate"}  # fmt: skip


def rows_for_treaty(
    treaty: Mapping[str, Any],
    body: bytes,
    countries: Mapping[int, Mapping[str, Any]],
    raw_sha: str,
    url: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Atlas rows of one instrument (``<ACRONYM>|field=...``) and a summary."""
    from reva.atlas.tables import json_obj

    doc = _json(body, f"GetCountriesForRatification {treaty['Id']}")
    if not isinstance(doc, list):
        raise OhchrParseError("GetCountriesForRatification: not a list")
    acr = str(treaty["Acronym"])
    asof = datetime.fromisoformat(str(treaty["LastSynced"])[:19]).date()
    rows: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()

    def add(
        i: int, area: str, name: str, field: str, d: date, text_: str, flags: dict[str, Any], keys: Sequence[str]
    ) -> None:
        rows.append(
            {
                "source": ATLAS_SOURCE, "dataset": DATASET, "indicator": f"{acr}|field={field}", "concept_id": None,
                "iso3": None, "provider_geo": area, "entity_qid": None, "period_start": d,
                "period_end": d + timedelta(days=1), "value": None, "value_text": text_[:MAX_TEXT], "unit": "status",
                "flags": json_obj({"country_name": name, **flags}), "raw_sha256": raw_sha,
                "selector": json_obj({"file": url, "index": i, "keys": list(keys)}),
            }
        )  # fmt: skip

    for i, r in enumerate(doc):
        missing = REQUIRED - set(r)
        if missing:
            raise OhchrParseError(f"{acr}: record {i} lacks {sorted(missing)}")
        if int(r["RatificationId"]) != int(treaty["Id"]):
            raise OhchrParseError(f"{acr}: record {i} belongs to ratification {r['RatificationId']}")
        c = countries.get(int(r["CountryId"]))
        if c is None:
            raise OhchrParseError(f"{acr}: unknown CountryId {r['CountryId']}")
        area, name = str(c["AreaId"]), str(c["CountryName"])
        st = COUNTRY_STATUS.get(int(r["CountryStatus"]), f"code:{r['CountryStatus']}")
        stats[st] += 1
        add(i, area, name, "status", asof, st, {"as_of": "treaty LastSynced " + str(treaty["LastSynced"]),
            "country_status_code": r["CountryStatus"]}, ["CountryStatus"])  # fmt: skip
        sig = ddmmyyyy(r["SignatoryDate"])
        if sig:
            add(i, area, name, "signature", sig, "signature", {"provider_text": r["SignatoryDate"]}, ["SignatoryDate"])
        con = ddmmyyyy(r["StatePartyDate"])
        if con:
            kind = STATE_PARTY_TYPE.get(int(r["StatePartyType"]), f"code:{r['StatePartyType']}")
            add(i, area, name, "consent", con, kind,
                {"provider_text": r["StatePartyDate"], "state_party_type": r["StatePartyType"],
                 "type_mapping": "established against UNTC MTDSG IV-4 (see atlas_ohchr module doc)"},
                ["StatePartyDate", "StatePartyType"])  # fmt: skip
        den = ddmmyyyy(r["DenunciationDate"])
        if den:
            add(i, area, name, "denunciation", den, "denunciation", {"provider_text": r["DenunciationDate"]},
                ["DenunciationDate"])  # fmt: skip
        for flag, dkey, tkey, field in (("IsIndividualComAllowed", "IndividualComDate", "IndividualComText",
                                         "individual_complaints"),
                                        ("IsInquiryProcAllowed", "InquiryProcDate", "InquiryProcText", "inquiry_procedure")):  # fmt: skip
            if r.get(flag) is True:
                d = ddmmyyyy(r.get(dkey))
                txt = text_of(r.get(tkey))
                add(i, area, name, field, d or asof, "accepted",
                    {"date_reported": d is not None, "text": txt[:1000], **({} if d else {"as_of": str(treaty["LastSynced"])})},
                    [flag, dkey, tkey])  # fmt: skip
                stats[field] += 1
        for key, field in (("Reservation", "reservations"), ("Declaration", "declarations")):
            txt = text_of(r.get(key))
            notes = text_of(r.get(key + "Notes"))
            if txt or notes:
                add(i, area, name, field, asof, txt or notes,
                    {"truncated": len(txt) > MAX_TEXT, "notes": notes[:1000], "as_of": str(treaty["LastSynced"])},
                    [key, key + "Notes"])  # fmt: skip
                stats[field] += 1
    summary = {
        "acronym": acr,
        "name": treaty["RatificationName"],
        "untc_mtdsg": ACRONYM_MTDSG.get(acr),
        "last_synced": treaty["LastSynced"],
        "records": len(doc),
        "counts": dict(stats),
    }
    return rows, summary


def frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA, empty_atlas_frame

    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA) if rows else empty_atlas_frame()


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.kernel.errors import SourceResponseError

    if not run.wants(DATASET):
        return {"skipped": True}
    try:
        t_raw = await run.get(URL_TREATIES, key="ratifications", media_types=("application/json",))
        treaties = parse_treaties(t_raw.body)
        c_raw = await run.get(URL_COUNTRIES, key="countries", media_types=("application/json",))
        countries = parse_countries(c_raw.body)
        unknown = sorted(str(t["Acronym"]) for t in treaties if str(t["Acronym"]) not in ACRONYM_MTDSG)
        if len(treaties) != EXPECTED_TREATIES or unknown:
            raise OhchrParseError(f"expected the 18 known instruments, got {len(treaties)} (unknown {unknown})")
        fetched = []
        for t in sorted(treaties, key=lambda x: int(x["Id"])):
            url = ratification_url(int(t["Id"]))
            raw = await run.get(url, key=f"ratification:{t['Id']}", media_types=("application/json",),
                                params={"acronym": t["Acronym"]})  # fmt: skip
            fetched.append((t, url, raw))
        rows: list[dict[str, Any]] = []
        summaries = []
        for t, url, raw in fetched:  # parse everything before archiving anything (schema drift archives nothing)
            r, s = rows_for_treaty(t, raw.body, countries, hashlib.sha256(raw.body).hexdigest(), url)
            rows += r
            summaries.append(s)
    except OhchrParseError as e:
        raise SourceResponseError(
            f"OHCHR dashboard data not parseable: {e}", retryable=False, kind="schema_drift"
        ) from e
    shas = {"t": run.archive(t_raw), "c": run.archive(c_raw)}
    for _, _, raw in fetched:
        if run.archive(raw) != hashlib.sha256(raw.body).hexdigest():
            raise SourceResponseError("archived raw sha256 differs from the body hash", retryable=False)
    raws = sorted({*shas.values(), *(row["raw_sha256"] for row in rows)})
    reg = run.register(
        dataset=DATASET, frame=frame(rows), raw_sha256s=raws, geo_scheme=GEO_SCHEME,
        coverage_note=f"OHCHR dashboard, {len(summaries)} instruments x {len(countries)} areas; status as of each "
        "instrument's LastSynced; AreaId PLS -> PSE (alias), ERU (European Union) unmapped. Not an open licence.",
        rights={"licence": LICENCE, "open_licence": False, "provider": "OHCHR", "source_key": SOURCE_KEY},
    )  # fmt: skip
    return {
        "atlas": {k: reg[k] for k in ("dataset_key", "row_count", "created")}
        | {"unmapped": reg.get("unmapped_provider_geo")},
        "areas": len(countries),
        "treaties": summaries,
    }
