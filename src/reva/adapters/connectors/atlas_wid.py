"""World Inequality Database (WID.world) key inequality series -> atlas table (V3, agent A-QOL).

Access: WID publishes one bulk archive, ``https://wid.world/bulk_download/wid_all_data.zip``
(882 MB on 2026-10-15, ETag ``"3493b842-65b0d5b44437d"``, last modified 2026-09-09; 848 members:
``WID_countries.csv``, then ``WID_data_XX.csv`` / ``WID_metadata_XX.csv`` per country/region XX;
6.9 GB uncompressed). There is no documented per-indicator bulk endpoint (the website's download
dialog points to the same archive). The server honours HTTP byte ranges (``Accept-Ranges: bytes``),
so this connector reads **only** what it needs, exactly as served:

1. the archive tail (``Range: bytes=-131072``) -> end-of-central-directory + central directory;
2. ``WID_countries.csv`` (4 KB compressed);
3. per crosswalk country: ``WID_data_XX.csv`` and ``WID_metadata_XX.csv`` (all members are deflated;
   ~640 MB compressed for the ~230 mappable countries in total, 2-9 MB each).

Every member request carries ``If-Range: <ETag of the tail>``: if WID replaces the archive mid-run,
the server answers 200 with the whole 882 MB file, which the size-capped client refuses (fail
closed; never a mix of two archive versions). Each archived raw artifact is exactly the bytes served
for its range (local file header + deflate stream); the parser checks the local header, the member
name, sizes and CRC-32 against the central directory.

Licence: WID's own pages and the archive README state no licence (checked 2026-10-15: wid.world/data,
methodology, codes dictionary, README.md inside the archive; the GitHub code repository has no
LICENSE file). Our World in Data's metadata for its WID-derived series records the origin licence as
"CC BY 4.0" with URL https://wid.world/. Recorded as ``licence_status = "cc_by_4.0_per_owid_metadata
_not_stated_by_wid"``; cite "World Inequality Database (WID), https://wid.world".

Series kept (:data:`SERIES`; ``j`` = equal-split adults, ``i`` = individuals, age ``992`` = adults
20+): pre-tax national income shares (bottom 50 %, middle 40 %, top 10 %, top 1 %) and Gini;
post-tax disposable income shares and Gini; net personal wealth shares and Gini; average national
income per adult. Values are fractions (shares, Gini in 0-1) or amounts in the currency named by
WID's metadata ``unit`` (price basis: WID technical note "Prices and currency conversions in
WID.world"). WID back-casts and extrapolates some series (its ``method`` metadata says so, e.g.
"Before 1980, series is constructed based on the trend observed in ..."): each cell keeps WID's
``data_quality`` code and the series' ``method``/``source`` text in ``flags``; nothing is dropped
or re-labelled as observed.
"""

from __future__ import annotations

import hashlib
import io
import re
import struct
import sys
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_qol_common as common
from reva.adapters.connectors.atlas_qol_common import AtlasBuild, canon
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import ConnectorSpec, FetchItem, RawAcquisition

CONNECTOR_NAME = "atlas.wid"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_wid/1"
SOURCE_KEY = "wid.bulk"
SOURCE = "wid"
DATASET = "wid_key"
HOST = "wid.world"
ARCHIVE = "wid_all_data.zip"
URL = f"https://{HOST}/bulk_download/{ARCHIVE}"
TAIL_BYTES = 131_072
MEMBER_SLACK = 1024  # local extra field allowance beyond the central-directory sizes
MAX_MEMBER_UNCOMPRESSED = 400_000_000
GEO_SCHEME = "wid"
LICENCE = "CC BY 4.0 per OWID origin metadata (https://wid.world/); not stated on WID's own pages"
ATTRIBUTION = "World Inequality Database (WID), https://wid.world"

#: (variable, percentile) -> indicator description
SERIES: dict[tuple[str, str], str] = {
    ("sptincj992", "p0p50"): "pre-tax national income share, bottom 50%",
    ("sptincj992", "p50p90"): "pre-tax national income share, middle 40%",
    ("sptincj992", "p90p100"): "pre-tax national income share, top 10%",
    ("sptincj992", "p99p100"): "pre-tax national income share, top 1%",
    ("gptincj992", "p0p100"): "pre-tax national income Gini",
    ("sdiincj992", "p0p50"): "post-tax disposable income share, bottom 50%",
    ("sdiincj992", "p90p100"): "post-tax disposable income share, top 10%",
    ("sdiincj992", "p99p100"): "post-tax disposable income share, top 1%",
    ("gdiincj992", "p0p100"): "post-tax disposable income Gini",
    ("shwealj992", "p0p50"): "net personal wealth share, bottom 50%",
    ("shwealj992", "p90p100"): "net personal wealth share, top 10%",
    ("shwealj992", "p99p100"): "net personal wealth share, top 1%",
    ("ghwealj992", "p0p100"): "net personal wealth Gini",
    ("anninci992", "p0p100"): "average national income per adult",
}
DATA_COLUMNS = ("country", "variable", "percentile", "year", "value", "age", "pop", "data_quality")
_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_MEMBER = re.compile(r"^WID_(data|metadata)_([A-Z]{2})\.csv$")


class WidError(SourceResponseError):
    def __init__(self, msg: str) -> None:
        super().__init__(msg, retryable=False, kind="parse_error")


# --------------------------------------------------------------------------- zip structures
@dataclass(frozen=True)
class Member:
    name: str
    method: int
    crc: int
    csize: int
    usize: int
    offset: int
    name_len: int
    extra_len: int


def parse_central_directory(tail: bytes, tail_start: int) -> tuple[list[Member], int]:
    """Members listed in the central directory found in ``tail`` (archive bytes from ``tail_start``).

    Returns (members, archive size implied by the EOCD). Zip64 archives are refused (typed error).
    """
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or len(tail) - i < 22:
        raise WidError("end of central directory not found in the archive tail")
    (_sig, disk, cd_disk, n_disk, n, cd_size, cd_off, clen) = struct.unpack("<IHHHHIIH", tail[i : i + 22])
    if disk != 0 or cd_disk != 0 or n_disk != n:
        raise WidError("multi-disk archives are not supported")
    if 0xFFFFFFFF in (cd_size, cd_off) or n == 0xFFFF:
        raise WidError("zip64 archive (not supported by this parser version)")
    p = cd_off - tail_start
    if p < 0 or p + cd_size > i:
        raise WidError("central directory is not inside the fetched tail (increase TAIL_BYTES)")
    out: list[Member] = []
    for _ in range(n):
        if tail[p : p + 4] != b"PK\x01\x02":
            raise WidError("bad central directory entry signature")
        h = struct.unpack("<IHHHHHHIIIHHHHHII", tail[p : p + 46])
        fnl, exl, cml = h[10], h[11], h[12]
        name = tail[p + 46 : p + 46 + fnl].decode("utf-8", "strict")
        out.append(Member(name, h[4], h[7], h[8], h[9], h[16], fnl, exl))
        p += 46 + fnl + exl + cml
    return out, tail_start + i + 22 + clen


def member_range(m: Member) -> tuple[int, int]:
    """Inclusive byte range covering the member's local header and compressed data."""
    return m.offset, m.offset + 30 + m.name_len + m.extra_len + MEMBER_SLACK + m.csize - 1


def inflate_member(body: bytes, served_start: int, m: Member) -> bytes:
    """The member's uncompressed bytes from a range response (checked against the directory)."""
    if served_start != m.offset:
        raise WidError(f"{m.name}: range starts at {served_start}, member at {m.offset}")
    if body[:4] != b"PK\x03\x04" or len(body) < 30:
        raise WidError(f"{m.name}: no local file header at the member offset")
    (_s, _v, flags, method, _t, _d, _crc, _cs, _us, fnl, exl) = struct.unpack("<IHHHHHIIIHH", body[:30])
    name = body[30 : 30 + fnl].decode("utf-8", "strict")
    if name != m.name or method != m.method:
        raise WidError(f"{m.name}: local header names {name!r} method {method}")
    if flags & 0x1:
        raise WidError(f"{m.name}: encrypted member")
    start = 30 + fnl + exl
    data = body[start : start + m.csize]
    if len(data) != m.csize:
        raise WidError(f"{m.name}: range holds {len(data)} of {m.csize} compressed bytes")
    if m.usize > MAX_MEMBER_UNCOMPRESSED:
        raise WidError(f"{m.name}: {m.usize} bytes uncompressed exceeds the bound")
    if method == 0:
        out = data
    elif method == 8:
        d = zlib.decompressobj(-15)
        try:
            out = d.decompress(data, MAX_MEMBER_UNCOMPRESSED + 1)
        except zlib.error as exc:
            raise WidError(f"{m.name}: corrupt deflate stream ({exc})") from exc
        if d.unconsumed_tail or len(out) > MAX_MEMBER_UNCOMPRESSED:
            raise WidError(f"{m.name}: inflates beyond the bound")
    else:
        raise WidError(f"{m.name}: unsupported compression method {method}")
    if len(out) != m.usize or (zlib.crc32(out) & 0xFFFFFFFF) != m.crc:
        raise WidError(f"{m.name}: size/CRC-32 mismatch with the central directory")
    return out


def content_range(raw: RawAcquisition) -> tuple[int, int, int]:
    cr = (raw.response_metadata.get("headers") or {}).get("content-range") or ""
    mm = _RANGE.match(cr.strip())
    if not mm:
        raise WidError(f"{raw.item.key}: response has no usable Content-Range ({cr!r})")
    return int(mm.group(1)), int(mm.group(2)), int(mm.group(3))


def _etag(raw: RawAcquisition) -> str | None:
    return (raw.response_metadata.get("headers") or {}).get("etag")


# --------------------------------------------------------------------------- CSV parsing
def parse_countries(text: str) -> list[dict[str, str]]:
    df = pl.read_csv(io.StringIO(text), separator=";", infer_schema=False)
    if "alpha2" not in df.columns:
        raise WidError("WID_countries.csv lacks alpha2")
    return df.to_dicts()


def parse_metadata(text: str) -> dict[str, dict[str, str]]:
    df = pl.read_csv(io.StringIO(text), separator=";", infer_schema=False, quote_char='"')
    need = {"variable", "unit", "source", "method", "shorttype"}
    if not need <= set(df.columns):
        raise WidError(f"WID metadata lacks {sorted(need - set(df.columns))}")
    wanted = {v for v, _ in SERIES}
    return {r["variable"]: r for r in df.filter(pl.col("variable").is_in(list(wanted))).to_dicts()}


def _unit(var: str, meta: dict[str, str] | None) -> str:
    t = (meta or {}).get("shorttype") or ""
    u = ((meta or {}).get("unit") or "").strip()
    if var.startswith("g"):
        return "Gini coefficient (0-1)"
    if var.startswith("s"):
        return "share (fraction of total, 0-1)"
    if u:
        return f"{u} (WID {t.lower() or 'amount'})"[:100]
    return "as published by WID (unit not stated)"


def parse_data(
    text: str, *, member: str, raw_sha: str, meta: dict[str, dict[str, str]], etag: str | None
) -> pl.DataFrame:
    df = pl.read_csv(io.StringIO(text), separator=";", infer_schema=False)
    if tuple(df.columns[: len(DATA_COLUMNS)]) != DATA_COLUMNS:
        raise WidError(f"{member}: columns {df.columns} != {DATA_COLUMNS}")
    df = df.with_row_index("row", offset=2)
    keys = pl.concat_str([pl.col("variable"), pl.lit("|"), pl.col("percentile")])
    wanted = [f"{v}|{p}" for v, p in SERIES]
    sel = df.filter(keys.is_in(wanted)).with_columns(pl.col("value").str.strip_chars())
    sel = sel.filter(pl.col("value").is_not_null() & (pl.col("value") != ""))
    if sel.height == 0:
        return common.frame([])
    bad = sel.filter(~pl.col("value").str.contains(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$"))
    if bad.height:
        # WID writes plain decimals; anything else is refused rather than coerced
        r = bad.row(0, named=True)
        raise WidError(f"{member}: row {r['row']}: value {r['value']!r} is not a strict decimal")
    bad_year = sel.filter(~pl.col("year").str.contains(r"^[0-9]{4}$"))
    if bad_year.height:
        raise WidError(f"{member}: non-calendar year {bad_year['year'][0]!r}")
    variables = sorted(set(sel["variable"].to_list()))
    unit_map = {v: _unit(v, meta.get(v)) for v in variables}
    meta_flags = {
        v: {
            "wid_source": ((meta.get(v) or {}).get("source") or "")[:600],
            "wid_method": ((meta.get(v) or {}).get("method") or "")[:1500],
            "series": "",
        }
        for v in variables
    }
    sel = sel.with_columns(pl.concat_str([pl.col("variable"), pl.lit("|"), pl.col("percentile")]).alias("indicator"))
    flag_rows = []
    for ind, dq in sel.select("indicator", "data_quality").unique().iter_rows():
        v, p = ind.split("|")
        f = {**meta_flags[v], "series": SERIES[(v, p)]}
        if dq not in (None, ""):
            f["data_quality"] = dq
        flag_rows.append({"indicator": ind, "data_quality": dq, "flags": canon(f)})
    fl = pl.DataFrame(flag_rows, schema={"indicator": pl.String, "data_quality": pl.String, "flags": pl.String})
    sel = sel.join(fl, on=["indicator", "data_quality"], how="left", nulls_equal=True)
    y = pl.col("year").cast(pl.Int32)
    return sel.select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(DATASET).alias("dataset"),
        pl.col("indicator"),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        pl.col("country").alias("provider_geo"),
        pl.lit(None, dtype=pl.String).alias("entity_qid"),
        pl.date(y, 1, 1).alias("period_start"),
        pl.date(y + 1, 1, 1).alias("period_end"),
        pl.col("value"),
        pl.lit(None, dtype=pl.String).alias("value_text"),
        pl.col("variable").replace_strict(unit_map, return_dtype=pl.String).alias("unit"),
        pl.col("flags"),
        pl.lit(raw_sha).alias("raw_sha256"),
        pl.format(
            '{{"archive":"{}","etag":{},"member":"{}","row":{}}}',
            pl.lit(ARCHIVE),
            pl.lit(canon_str(etag)),
            pl.lit(member),
            pl.col("row"),
        ).alias("selector"),
    )


def canon_str(s: str | None) -> str:
    import json

    return json.dumps(s)


# --------------------------------------------------------------------------- connector
class WidConnector:
    def __init__(self, countries: Sequence[str] | None = None) -> None:
        """``countries``: WID alpha-2 codes to fetch (default: every code mapping to the crosswalk)."""
        self.countries = tuple(sorted({c.upper() for c in countries})) if countries else None
        self._members: dict[str, Member] = {}
        self._etag: str | None = None

    def describe(self) -> ConnectorSpec:
        return common.atlas_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="World Inequality Database (WID.world)",
            licence=LICENCE,
            allowed_hosts=(HOST,),
            dataset_ids=(ARCHIVE,),
            field_mappings={"atlas_tables": [f"{SOURCE}.{DATASET}"], "series": [f"{v}|{p}" for v, p in SERIES]},
            units={},
            geography=self.countries or ("all",),
            time_semantics="calendar years",
            lineage_family="wid",
            cadence_seconds=30 * common.DAY,
            release_aware=True,
            revision_strategy="archive ETag; a new archive = new raw members = new atlas table version",
            max_response_bytes=20_000_000,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=common.code_digest(sys.modules[__name__]),
        )

    def fetch_items(self) -> tuple[FetchItem, ...]:
        return (
            FetchItem(
                key="tail",
                url=URL,
                expected_media_types=("application/zip", "application/octet-stream"),
                max_bytes=TAIL_BYTES,
                params={"range": f"-{TAIL_BYTES}"},
            ),
        )

    def _member_item(self, name: str) -> FetchItem:
        m = self._members.get(name)
        if m is None:
            raise WidError(f"archive has no member {name}")
        a, b = member_range(m)
        return FetchItem(
            key=f"member:{name}",
            url=URL,
            expected_media_types=("application/zip", "application/octet-stream"),
            max_bytes=b - a + 1,
            params={"range": f"{a}-{b}", "member": name, "if_range": self._etag},
        )

    def follow_up(self, raw: RawAcquisition) -> tuple[FetchItem, ...]:
        if raw.item.key == "tail":
            start, _end, total = content_range(raw)
            members, size = parse_central_directory(raw.body, start)
            if size != total:
                raise WidError(f"EOCD implies {size} bytes, server reports {total}")
            self._members = {m.name: m for m in members}
            self._etag = _etag(raw)
            if not self._etag:
                raise WidError("archive response has no ETag (cannot pin member ranges)")
            return (self._member_item("WID_countries.csv"),)
        if raw.item.key == "member:WID_countries.csv":
            m = self._members["WID_countries.csv"]
            text = inflate_member(raw.body, content_range(raw)[0], m).decode("utf-8")
            codes = self.target_codes(parse_countries(text))
            items: list[FetchItem] = []
            for c in codes:
                for kind in ("data", "metadata"):
                    name = f"WID_{kind}_{c}.csv"
                    if name in self._members:
                        items.append(self._member_item(name))
            return tuple(items)
        return ()

    def target_codes(self, countries: list[dict[str, str]]) -> list[str]:
        from reva.atlas.tables import country_iso3

        codes = sorted({r["alpha2"] for r in countries if r.get("alpha2") and len(r["alpha2"]) == 2})
        mapped = [c for c in codes if country_iso3(c, GEO_SCHEME) is not None]
        if self.countries is not None:
            mapped = [c for c in mapped if c in self.countries]
        return mapped

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        from reva.adapters.http.safe_client import redact_url

        headers = {"Range": f"bytes={item.params['range']}"}
        if item.params.get("if_range"):
            headers["If-Range"] = item.params["if_range"]
        r = await http.get(item.url, headers=headers, max_bytes=item.max_bytes, accept_status=(206,))
        meta = r.response_metadata()
        meta["headers"] = {**meta.get("headers", {}), "content-range": r.headers.get("content-range", "")}
        fp = hashlib.sha256(f"GET {redact_url(item.url)} Range: bytes={item.params['range']}".encode()).hexdigest()
        return RawAcquisition(
            item=item,
            body=r.body,
            media_type=r.media_type or "application/octet-stream",
            retrieved_at=r.retrieved_at,
            response_metadata=meta,
            request_fingerprint=fp,
            origin=getattr(http, "origin", "live"),
        )

    def build(self, raws: Sequence[RawAcquisition]) -> list[AtlasBuild]:
        tail = next((r for r in raws if r.item.key == "tail"), None)
        if tail is None:
            raise WidError("missing raw item tail")
        start, _e, total = content_range(tail)
        members, size = parse_central_directory(tail.body, start)
        if size != total:
            raise WidError(f"EOCD implies {size} bytes, server reports {total}")
        by_name = {m.name: m for m in members}
        etag = _etag(tail)
        sha = {r.item.key: common.sha256_hex(r.body) for r in raws}
        texts: dict[str, tuple[str, RawAcquisition]] = {}
        for r in raws:
            if not r.item.key.startswith("member:"):
                continue
            if _etag(r) != etag:
                raise WidError(f"{r.item.key}: ETag {_etag(r)!r} differs from the archive tail's {etag!r}")
            name = r.item.params["member"]
            body = inflate_member(r.body, content_range(r)[0], by_name[name])
            texts[name] = (body.decode("utf-8"), r)
        frames: list[pl.DataFrame] = []
        fetched: list[str] = []
        empty: list[str] = []
        for name in sorted(texts):
            mm = _MEMBER.match(name)
            if not mm or mm.group(1) != "data":
                continue
            code = mm.group(2)
            meta_name = f"WID_metadata_{code}.csv"
            meta = parse_metadata(texts[meta_name][0]) if meta_name in texts else {}
            text, raw = texts[name]
            f = parse_data(text, member=name, raw_sha=sha[raw.item.key], meta=meta, etag=etag)
            fetched.append(code)
            if f.height == 0:
                empty.append(code)
                continue
            frames.append(f)
        if not frames:
            raise WidError("no WID series rows parsed")
        frame = pl.concat(frames)
        used = sorted(set(sha.values()))
        rep = {
            "archive_etag": etag,
            "archive_bytes": total,
            "members_in_archive": len(members),
            "countries_fetched": len(fetched),
            "countries_without_selected_series": empty,
            "series": [f"{v}|{p}" for v, p in SERIES],
        }
        return [
            AtlasBuild(
                source=SOURCE,
                dataset=DATASET,
                frame=frame,
                raw_sha256s=used,
                parser_version=PARSER_VERSION,
                coverage_note=(
                    f"WID bulk archive {ARCHIVE} (ETag {etag}, {total} bytes) read by byte ranges: central directory, "
                    f"WID_countries.csv and data+metadata members of {len(fetched)} countries. 14 key series "
                    "(income/wealth shares and Gini, average national income per adult). data_quality and WID "
                    "method/source text kept per cell; back-cast/extrapolated years are WID's own."
                ),
                geo_scheme=GEO_SCHEME,
                rights={
                    "licence": LICENCE,
                    "licence_status": "cc_by_4.0_per_owid_metadata_not_stated_by_wid",
                    "attribution": ATTRIBUTION,
                    "licence_checked": "2026-10-15",
                },
                report=rep,
            )
        ]


__all__ = [
    "CONNECTOR_NAME",
    "SERIES",
    "Member",
    "WidConnector",
    "inflate_member",
    "member_range",
    "parse_central_directory",
    "parse_data",
    "parse_metadata",
]
