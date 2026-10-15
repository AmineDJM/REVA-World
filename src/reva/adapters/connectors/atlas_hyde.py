"""HYDE 3.2.1 country summaries (population, urban/rural population, land use) -> atlas table
``hyde.hyde321`` (V3, agent A-HISTECON).

Source and access
-----------------
Klein Goldewijk, K., A. Beusen, J. Doelman and E. Stehfest (2017), "Anthropogenic land use estimates for the
Holocene - HYDE 3.2", Earth System Science Data 9, 927-953; data: DANS Data Station Archaeology
``doi:10.17026/dans-25g-gez3`` (v2, 2017 release HYDE 3.2.1). HYDE 3.3 (2023) is served by Utrecht
University's Yoda portal behind an Anubis proof-of-work bot challenge, which this connector does not try to
pass; 3.2.1 is the latest release reachable programmatically.

Licence: the dataset record says **CC0-1.0**, the release readme inside the deposit says "The Creative Commons
License (CC BY 3.0) applies to all of the HYDE data". The stricter one (**CC BY 3.0**, attribution) is applied
and both statements are recorded.

The country summary tables (``baseline/txt/*_c.txt``) live inside the 5.3 GB ``HYDE3_2_1-baseline.zip``. The
connector does **not** download the archive: it makes one HTTP ``Range`` request for the last
``TAIL_BYTES`` of the file (the 35 ``txt`` members are stored just before the central directory), reads the
zip64 end records and central directory from that tail, and inflates the wanted members (zip method 9,
Deflate64 -> :mod:`atlas_inflate64`), checking each member's CRC-32. Three requests per run (record, readme,
tail), ~1.5 MB.

What is parsed
--------------
Baseline scenario, one indicator per summary file (``SUMMARIES``): one row per country code (ISO 3166
numeric, the HYDE ``iso_cr`` grid codes) and HYDE time step. Codes whose row is zero in every year of every
file are placeholders (the files list codes 1-894) and are not emitted; a zero for a real country is kept as
reported. The ``Total`` row is kept as provider_geo ``world``. **BCE time steps and year 0 cannot be
represented** in the atlas period columns (dates from year 1 CE): they are not loaded and are counted in the
report (10000 BCE - 0 CE: 11 steps). HYDE values are model estimates (allocation of historical population
and land-use estimates to a 5' grid, summed per country); every row says so (``hyde_modelled``).
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

from reva.adapters.connectors.atlas_history_common import (
    DAY,
    AtlasBuild,
    canon,
    code_digest,
    decimal_text,
    frame,
    historical_qid,
    ingest_with_run,
    sha256_hex,
    source_spec,
    year_period,
)
from reva.adapters.connectors.atlas_inflate64 import InflateError, inflate
from reva.adapters.connectors.base import to_raw
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

NAME = "atlas.hyde.hyde321"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_hyde/1"
SOURCE_KEY = "hyde.dans"
ATLAS_SOURCE = "hyde"
ATLAS_DATASET = "hyde321"
HOST = "archaeology.datastations.nl"
DOI = "doi:10.17026/dans-25g-gez3"
RECORD_URL = f"https://{HOST}/api/datasets/:persistentId/?persistentId={DOI}"
FILE_URL = f"https://{HOST}/api/access/datafile/{{id}}"
BASELINE = "HYDE3_2_1-baseline.zip"
README = "readme_release_HYDE3.2.1.txt"
TAIL_BYTES = 1_500_000
MAX_MEMBER_OUT = 8_000_000

#: member -> (indicator, unit, note)
SUMMARIES: dict[str, tuple[str, str, str]] = {
    "baseline/txt/popc_c.txt": ("popc", "inhabitants", "total population (popc: population counts)"),
    "baseline/txt/urbc_c.txt": ("urbc", "inhabitants", "urban population (urbc)"),
    "baseline/txt/rurc_c.txt": ("rurc", "inhabitants", "rural population (rurc)"),
    "baseline/txt/cropland_c.txt": ("cropland", "km2", "total cropland area"),
    "baseline/txt/grazing_c.txt": ("grazing", "km2", "total land used for grazing (pasture + rangeland)"),
    "baseline/txt/pasture_c.txt": ("pasture", "km2", "pasture (grazing land with aridity index > 0.5)"),
    "baseline/txt/rangeland_c.txt": ("rangeland", "km2", "rangeland (less or not managed grazing land)"),
    "baseline/txt/tot_irri_c.txt": ("tot_irri", "km2", "total actual irrigated area"),
    "baseline/txt/uopp_c.txt": ("uopp", "km2", "total built-up area"),
}
UNIT_NOTE = (
    "The release readme lists 'pop_c.txt (total population per country over time, in 1000)'; the archive holds "
    "popc_c.txt / urbc_c.txt / rurc_c.txt whose values are inhabitants (world total 4,432,267 in 10000 BCE; "
    "France 2017 6.5497964E+07), consistent with the readme's grid unit for popc ('inhabitants/gridcell')."
)
RIGHTS: dict[str, str] = {
    "licence": "CC BY 3.0",
    "licence_url": "https://creativecommons.org/licenses/by/3.0/",
    "licence_basis": (
        "release readme: 'The Creative Commons License (CC BY 3.0) applies to all of the HYDE data'; the DANS "
        "dataset record states CC0-1.0 -- the stricter (attribution) licence is applied"
    ),
    "attribution": (
        "Klein Goldewijk, K., Beusen, A., Doelman, J. and Stehfest, E. (2017), Anthropogenic land use estimates "
        "for the Holocene - HYDE 3.2, Earth Syst. Sci. Data 9, 927-953. Data: DANS, doi:10.17026/dans-25g-gez3"
    ),
}


class HydeParseError(SourceResponseError):
    pass


def _err(msg: str) -> HydeParseError:
    return HydeParseError(msg, retryable=False, kind="schema_drift")


# --------------------------------------------------------------------------- record / readme
def parse_record(body: bytes) -> dict[str, Any]:
    try:
        lv = json.loads(body)["data"]["latestVersion"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err("dataset record is not the expected Dataverse JSON") from e
    files = {(f.get("dataFile") or {}).get("filename"): f.get("dataFile") or {} for f in lv.get("files") or []}
    if BASELINE not in files or README not in files:
        raise _err(f"record lacks {BASELINE} or {README}")
    lic = lv.get("license")
    return {
        "version": f"{lv.get('versionNumber')}.{lv.get('versionMinorNumber')}",
        "record_licence": lic.get("name") if isinstance(lic, dict) else lic,
        "baseline_id": int(files[BASELINE]["id"]),
        "baseline_size": int(files[BASELINE]["filesize"]),
        "readme_id": int(files[README]["id"]),
        "readme_sha1": (files[README].get("checksum") or {}).get("value"),
        "production_date": lv.get("productionDate"),
    }


def check_readme(body: bytes) -> str:
    text = body.decode("latin-1")
    m = re.search(r"Creative Commons License \(([^)]+)\) applies to all of the HYDE data", text)
    if not m:
        raise SourceResponseError("HYDE readme no longer states the CC licence clause", retryable=False, kind="licence")
    if m.group(1) != "CC BY 3.0":
        raise SourceResponseError(f"HYDE readme licence changed to {m.group(1)!r}", retryable=False, kind="licence")
    return m.group(1)


# --------------------------------------------------------------------------- zip tail
def zip_tail_members(tail: bytes, wanted: Sequence[str], *, file_size: int | None = None) -> dict[str, bytes]:
    """Inflate ``wanted`` members from the last bytes of a (zip64) archive; CRC-checked."""
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or len(tail) - i < 22:
        raise _err("no end-of-central-directory record in the tail")
    loc = tail.rfind(b"PK\x06\x07", 0, i)
    if loc >= 0:
        (z64_off,) = struct.unpack("<Q", tail[loc + 8 : loc + 16])
        j = tail.rfind(b"PK\x06\x06", 0, loc)
        if j < 0:
            raise _err("zip64 locator without a zip64 end record in the tail")
        base = z64_off - j
        _, _, _, _, _, _, _n_disk, n_tot, cd_size, cd_off = struct.unpack("<IQHHIIQQQQ", tail[j : j + 56])
    else:
        _, _, _, _, n_tot, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[i : i + 22])
        if file_size is None:
            raise _err("non-zip64 archive tail needs the file size")
        base = file_size - len(tail)
    if file_size is not None and base != file_size - len(tail):
        raise _err("tail offset inconsistent with the file size")
    p = cd_off - base
    if p < 0:
        raise _err("central directory starts before the tail: increase TAIL_BYTES")
    entries: dict[str, tuple[int, int, int, int, int]] = {}
    for _ in range(n_tot):
        if tail[p : p + 4] != b"PK\x01\x02":
            raise _err("bad central directory entry")
        (_, _, _, _flag, meth, _, _, crc, csz, usz, nl, el, cl, _, _, _, off) = struct.unpack(
            "<IHHHHHHIIIHHHHHII", tail[p : p + 46]
        )
        name = tail[p + 46 : p + 46 + nl].decode("utf-8", "replace")
        extra = tail[p + 46 + nl : p + 46 + nl + el]
        q = 0
        while q + 4 <= len(extra):
            hid, hsz = struct.unpack("<HH", extra[q : q + 4])
            body = extra[q + 4 : q + 4 + hsz]
            if hid == 1:
                k = 0
                if usz == 0xFFFFFFFF:
                    (usz,) = struct.unpack("<Q", body[k : k + 8])
                    k += 8
                if csz == 0xFFFFFFFF:
                    (csz,) = struct.unpack("<Q", body[k : k + 8])
                    k += 8
                if off == 0xFFFFFFFF:
                    (off,) = struct.unpack("<Q", body[k : k + 8])
            q += 4 + hsz
        entries[name] = (meth, crc, csz, usz, off)
        p += 46 + nl + el + cl
    if p - (cd_off - base) != cd_size:
        raise _err("central directory size mismatch")
    out: dict[str, bytes] = {}
    for name in wanted:
        if name not in entries:
            raise _err(f"member {name} not in the archive")
        meth, crc, csz, usz, off = entries[name]
        lp = off - base
        if lp < 0:
            raise _err(f"member {name} starts before the tail: increase TAIL_BYTES")
        if tail[lp : lp + 4] != b"PK\x03\x04":
            raise _err(f"bad local header for {name}")
        nl, el = struct.unpack("<HH", tail[lp + 26 : lp + 30])
        data = tail[lp + 30 + nl + el : lp + 30 + nl + el + csz]
        if len(data) != csz:
            raise _err(f"member {name} truncated in the tail")
        if usz > MAX_MEMBER_OUT:
            raise _err(f"member {name} larger than the bound")
        try:
            if meth == 0:
                raw = data
            elif meth in (8, 9):
                raw = inflate(data, deflate64=meth == 9, max_out=MAX_MEMBER_OUT)
            else:
                raise _err(f"member {name}: unsupported compression method {meth}")
        except InflateError as e:
            raise _err(f"member {name}: {e}") from e
        if len(raw) != usz or zlib.crc32(raw) != crc:
            raise _err(f"member {name}: size/CRC check failed")
        out[name] = raw
    return out


# --------------------------------------------------------------------------- summaries
def parse_summary(text: str, member: str) -> tuple[list[int], dict[str, list[str]]]:
    lines = [ln.split() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0][0] != "region":
        raise _err(f"{member}: header does not start with 'region'")
    years = []
    for y in lines[0][1:]:
        if not re.fullmatch(r"-?\d{1,5}", y):
            raise _err(f"{member}: year header {y!r}")
        years.append(int(y))
    rows: dict[str, list[str]] = {}
    for r in lines[1:]:
        if len(r) != len(years) + 1:
            raise _err(f"{member}: row {r[0]} has {len(r) - 1} values for {len(years)} years")
        if r[0] in rows:
            raise _err(f"{member}: duplicate region {r[0]}")
        rows[r[0]] = r[1:]
    return years, rows


def _is_zero(v: str) -> bool:
    d = decimal_text(v)
    return d is not None and float(d) == 0.0


def atlas_rows(
    members: Mapping[str, bytes], *, raw_sha256: str, record: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    parsed = {m: parse_summary(b.decode("latin-1"), m) for m, b in members.items()}
    live_codes: set[str] = set()
    for _years, rows in parsed.values():
        for code, vals in rows.items():
            if code != "Total" and not all(_is_zero(v) for v in vals):
                live_codes.add(code)
    out: list[dict[str, Any]] = []
    excluded_bce = 0
    bad: list[dict[str, Any]] = []
    for member, (years, rows) in parsed.items():
        ind, unit, note = SUMMARIES[member]
        for code, vals in rows.items():
            if code != "Total" and code not in live_codes:
                continue
            geo = "world" if code == "Total" else code
            iso3 = None if code == "Total" else country_iso3(code, "m49")
            q = None if iso3 or code == "Total" else historical_qid("hyde", code)
            for k, (y, v) in enumerate(zip(years, vals, strict=True)):
                if y < 1:
                    excluded_bce += 1
                    continue
                d = decimal_text(v)
                if d is None:
                    bad.append({"member": member, "region": code, "year": y, "text": v[:20]})
                    continue
                flags: dict[str, Any] = {
                    "hyde_modelled": True,
                    "scenario": "baseline",
                    "release": "HYDE 3.2.1",
                    "variable": note,
                    "licence": RIGHTS["licence"],
                    "dans_version": record.get("version"),
                }
                if ind in ("popc", "urbc", "rurc"):
                    flags["unit_note"] = UNIT_NOTE
                if code == "Total":
                    flags["aggregate"] = "world (HYDE Total row)"
                if q is not None:
                    flags["entity_mapping"] = {"qid": q["qid"], "review": q.get("review_status", "agent_mapped")}
                start, end = year_period(y)
                out.append(
                    {
                        "source": ATLAS_SOURCE,
                        "dataset": ATLAS_DATASET,
                        "indicator": ind,
                        "concept_id": None,
                        "iso3": iso3,
                        "provider_geo": geo,
                        "entity_qid": q["qid"] if q else None,
                        "period_start": start,
                        "period_end": end,
                        "value": d,
                        "value_text": None,
                        "unit": unit,
                        "flags": canon(flags),
                        "raw_sha256": raw_sha256,
                        "selector": canon(file=f"{BASELINE}!{member}", region=code, column=k + 1, year=y),
                    }
                )
    years0 = next(iter(parsed.values()))[0] if parsed else []
    report = {
        "members": sorted(members),
        "countries": len(live_codes),
        "excluded_bce_or_year0_cells": excluded_bce,
        "bce_steps": [y for y in years0 if y < 1],
        "bad_cells": bad[:20],
        "unmapped_codes": sorted(c for c in live_codes if country_iso3(c, "m49") is None),
    }
    return out, report


class HydeConnector:
    """HYDE 3.2.1 baseline country summaries via one Range request on the DANS archive."""

    def __init__(self, *, tail_bytes: int = TAIL_BYTES) -> None:
        self.tail_bytes = tail_bytes  # smaller only for synthetic test archives

    def describe(self) -> Any:
        return atlas_source()

    def fetch_items(self) -> tuple[FetchItem, ...]:
        return (FetchItem(key="hyde.record", url=RECORD_URL, max_bytes=2_000_000),)

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]:
        if raw.item.key != "hyde.record":
            return ()
        rec = parse_record(raw.body)
        return (
            FetchItem(
                key="hyde.readme",
                url=FILE_URL.format(id=rec["readme_id"]),
                max_bytes=200_000,
                expected_media_types=("text/plain", "application/octet-stream"),
                params={"sha1": rec["readme_sha1"]},
            ),
            FetchItem(
                key="hyde.baseline_tail",
                url=FILE_URL.format(id=rec["baseline_id"]),
                max_bytes=self.tail_bytes + 1,
                expected_media_types=("application/zip", "application/octet-stream"),
                params={"range": f"bytes=-{self.tail_bytes}", "file_size": rec["baseline_size"]},
            ),
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        rng = item.params.get("range")
        if rng:
            r = await http.get(item.url, max_bytes=item.max_bytes, headers={"Range": rng}, accept_status=(206,))
        else:
            r = await http.get(item.url, max_bytes=item.max_bytes)
        return to_raw(item, r, getattr(http, "origin", "live"))

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]:
        import hashlib

        by = {r.item.key: r for r in raws}
        if set(by) != {"hyde.record", "hyde.readme", "hyde.baseline_tail"}:
            raise _err(f"expected record, readme and archive tail, got {sorted(by)}")
        rec = parse_record(by["hyde.record"].body)
        readme = by["hyde.readme"].body
        if rec["readme_sha1"] and hashlib.sha1(readme).hexdigest() != rec["readme_sha1"]:  # noqa: S324
            raise _err("readme SHA-1 differs from the dataset record")
        check_readme(readme)
        tail = by["hyde.baseline_tail"]
        want = min(self.tail_bytes, rec["baseline_size"])
        if len(tail.body) != want:
            raise _err(f"archive tail is {len(tail.body)} bytes, expected {want} (Range not honoured?)")
        members = zip_tail_members(tail.body, sorted(SUMMARIES), file_size=rec["baseline_size"])
        sha = sha256_hex(tail.body)
        rows, report = atlas_rows(members, raw_sha256=sha, record=rec)
        report["record"] = rec
        note = (
            f"HYDE 3.2.1 baseline country summaries ({len(SUMMARIES)} variables) from {BASELINE} "
            f"(DANS {DOI}, Range request on the archive tail); CE time steps 100-2017; BCE steps not loaded; "
            "modelled estimates."
        )
        used = [sha, sha256_hex(by["hyde.record"].body), sha256_hex(readme)]
        return [
            AtlasBuild(
                ATLAS_SOURCE,
                ATLAS_DATASET,
                frame(rows),
                used,
                PARSER_VERSION,
                note,
                geo_scheme=None,
                rights={**RIGHTS, "record_licence": rec["record_licence"], "doi": DOI},
                report=report,
            )
        ]


def atlas_source() -> Any:
    """The atlas platform declaration (``reva.atlas.sources``)."""
    return source_spec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        datasets=(ATLAS_DATASET,),
        provider="PBL Netherlands Environmental Assessment Agency / Utrecht University (HYDE), via DANS",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST, "objectstore.surf.nl"),
        dataset_ids=(DOI,),
        field_mappings={m: v[0] for m, v in SUMMARIES.items()},
        units={v[0]: v[1] for v in SUMMARIES.values()},
        geography=("ISO 3166 numeric codes of the HYDE iso_cr grid",),
        time_semantics="HYDE time steps (100-year to 1700, 10-year to 2000, yearly to 2017); BCE steps not loaded",
        lineage_family="hyde",
        cadence_seconds=180 * DAY,
        revision_strategy="new DANS dataset version -> new atlas table version",
        planned_requests=3,
        max_response_bytes=4_000_000,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    out = await ingest_with_run(run, HydeConnector())
    return {k: v for k, v in out.items() if not k.startswith("_")}
