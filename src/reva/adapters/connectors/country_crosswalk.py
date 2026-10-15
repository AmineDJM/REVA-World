"""Country/economy crosswalk built from the providers' own reference lists (V2 step 1).

Inputs (archived raw bytes under ``registry/reference/``, digests in
``registry/reference/manifest.yaml`` and in the crosswalk itself):

* UNSD M49 overview (``unstats.un.org/unsd/methodology/m49/overview/``): the
  authority for M49 numeric codes and their ISO 3166 alpha-2/alpha-3 codes.
* UN Comtrade ``Reporters.json`` and ``partnerAreas.json``: provider reporter and
  partner codes (M49-based, but not M49: France 251 vs M49 250, USA 842 vs 840).
* World Bank ``/v2/country`` (all 296 rows): economies and aggregates.

Output: ``registry/entities/country_crosswalk.json``, one entity per ISO 3166
alpha-3 code listed by M49, plus World Bank economies without an ISO 3166 code
(``CHI`` Channel Islands, ``XKX`` Kosovo: keyed by the World Bank code, flagged
``not_iso3166``). World Bank aggregates (region "Aggregates": regions, income
groups, lending groups) are excluded and listed.

Mapping rules (deterministic, conservative; nothing is guessed):

1. A Comtrade *reporter* code maps to an entity only when it is not a group, is
   valid at some point in the coverage window, its label is not a "nes" area,
   its provider ISO3 field is an ISO 3166 alpha-3 code listed by M49, and it is
   the only such reporter for that ISO3. Anything else is listed under
   ``unmapped_provider_codes`` with flags (``group``, ``nes_area``,
   ``historical``, ``provider_iso3_not_iso3166``, ``ambiguous_iso3``) and is
   never mapped to an ISO3 -- e.g. 490 "Other Asia, nes", 97 "European Union",
   899 "Areas, nes" (partner), "(...1990)" former entities.
2. A reporter whose provider note lists more than its label (``"France,
   Monaco"``, ``"USA, Puerto Rico and US Virgin Islands"``, ``"Switzerland,
   Liechtenstein"``, ``"Norway, Svalbard and Jan Mayen"``) is mapped, as the
   reviewed FRA/USA aliases already are, but flagged
   ``comtrade_territory_note_lists_additional_areas`` with the note recorded:
   its trade figures cover a wider territory than the World Bank economy.
3. Partner codes are recorded per entity for information only (same rules as
   reporters; the provider ISO3 field of partner "nes" areas is unreliable, e.g.
   492 "Europe EU, nes" carries "MCO"). They are not seeded as aliases: bilateral
   rows keep the provider partner code and resolve to provider-declared partner
   entities (``reva.cli.seed``), so no partner territory is merged into a country.

Entities defined in ``registry/entities/countries.yaml`` (human-reviewed) keep
their reviewed aliases; the crosswalk records them with ``reviewed_entry: true``
and the seed does not add crosswalk aliases for them.

CLI (maintainer tool, the only network path is ``fetch`` through SafeHttpClient)::

    python -m reva.adapters.connectors.country_crosswalk fetch   # refresh raw archives
    python -m reva.adapters.connectors.country_crosswalk build   # rebuild the JSON
    python -m reva.adapters.connectors.country_crosswalk verify  # digests + rebuild equality
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

REGISTRY_ROOT = Path(__file__).resolve().parents[4] / "registry"
REFERENCE_DIR = REGISTRY_ROOT / "reference"
CROSSWALK_PATH = REGISTRY_ROOT / "entities" / "country_crosswalk.json"
REVIEWED_ENTITIES_PATH = REGISTRY_ROOT / "entities" / "countries.yaml"

SCHEMA = "reva-country-crosswalk/1"
CROSSWALK_VERSION = "1.0.0"
#: Alias rows seeded from the crosswalk carry this mapping_version (world.alias).
ALIAS_MAPPING_VERSION = f"crosswalk-{CROSSWALK_VERSION}"
WINDOW = (2019, 2024)


@dataclass(frozen=True)
class SourceFile:
    name: str
    file: str  # archive file under registry/reference/
    url: str
    gzip: bool = False


SOURCES: tuple[SourceFile, ...] = (
    SourceFile("unsd_m49", "unsd_m49_overview.html.gz", "https://unstats.un.org/unsd/methodology/m49/overview/",
               gzip=True),
    SourceFile("comtrade_reporters", "comtrade_Reporters.json",
               "https://comtradeapi.un.org/files/v1/app/reference/Reporters.json"),
    SourceFile("comtrade_partners", "comtrade_partnerAreas.json",
               "https://comtradeapi.un.org/files/v1/app/reference/partnerAreas.json"),
    SourceFile("worldbank_countries", "worldbank_country.json",
               "https://api.worldbank.org/v2/country?format=json&per_page=400"),
)
ALLOWED_HOSTS = ("unstats.un.org", "comtradeapi.un.org", "api.worldbank.org")


class CrosswalkError(ValueError):
    """The archived sources or the crosswalk file are inconsistent (fails closed)."""


# --------------------------------------------------------------------------- parsing
class _M49Table(HTMLParser):
    """Rows of the English M49 table (``<table id="downloadTableEN">``)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table" and (a.get("id") or "").strip() == "downloadTableEN":
            self.in_table = True
        elif self.in_table and tag == "tr":
            self._row = []
        elif self.in_table and tag == "td" and self._row is not None:
            self._cell = ""

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag == "table":
            self.in_table = False
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(" ".join(self._cell.split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell += data


M49_COLUMNS = ("Country or Area", "M49 Code", "ISO-alpha2 Code", "ISO-alpha3 Code", "Region Name",
               "Sub-region Name")


def parse_m49(html: bytes) -> list[dict[str, str]]:
    p = _M49Table()
    p.feed(html.decode("utf-8"))
    if not p.rows:
        raise CrosswalkError("M49 overview: English table not found (page structure changed)")
    header, body = p.rows[0], p.rows[1:]
    missing = [c for c in M49_COLUMNS if c not in header]
    if missing:
        raise CrosswalkError(f"M49 overview: columns {missing} absent (page structure changed)")
    rows = [dict(zip(header, r, strict=False)) for r in body]
    if len(rows) < 200:
        raise CrosswalkError(f"M49 overview: only {len(rows)} rows parsed; refusing a partial table")
    return rows


def _results(data: bytes, what: str) -> list[dict[str, Any]]:
    doc = json.loads(data)
    if not isinstance(doc, dict) or not isinstance(doc.get("results"), list):
        raise CrosswalkError(f"{what}: expected an object with a 'results' list")
    return doc["results"]


def parse_worldbank(data: bytes) -> list[dict[str, Any]]:
    doc = json.loads(data)
    if not (isinstance(doc, list) and len(doc) == 2 and isinstance(doc[0], dict) and isinstance(doc[1], list)):
        raise CrosswalkError("World Bank country list: unexpected payload shape")
    header, rows = doc
    if int(header.get("pages", 0)) != 1 or int(header.get("total", -1)) != len(rows):
        raise CrosswalkError(f"World Bank country list is paged or truncated (header {header}, {len(rows)} rows)")
    return rows


def _year(value: Any) -> int | None:
    return int(str(value)[:4]) if value else None


def _active_in_window(effective: Any, expired: Any) -> bool:
    start, end = _year(effective), _year(expired)
    return (start is None or start <= WINDOW[1]) and (end is None or end >= WINDOW[0])


def _is_nes(label: str) -> bool:
    words = label.lower().replace(",", " ").split()
    return "nes" in words


def _code_flags(*, label: str, iso3_field: str, is_group: bool, effective: Any, expired: Any,
                iso3_known: set[str]) -> list[str]:
    flags = []
    if is_group:
        flags.append("group")
    if _is_nes(label):
        flags.append("nes_area")
    if not _active_in_window(effective, expired):
        flags.append("historical")
    if (iso3_field or "").strip() not in iso3_known:
        flags.append("provider_iso3_not_iso3166")
    return flags


# --------------------------------------------------------------------------- build
def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_source(src: SourceFile, root: Path = REFERENCE_DIR) -> bytes:
    data = (root / src.file).read_bytes()
    return gzip.decompress(data) if src.gzip else data


def _reviewed_keys(path: Path = REVIEWED_ENTITIES_PATH) -> set[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {e["key"] for e in doc["entities"] if e.get("kind") == "entity.country"}


def build(raw: dict[str, bytes], *, retrieved: dict[str, str], reviewed_keys: set[str]) -> dict[str, Any]:
    """Pure: the crosswalk document from the four raw reference payloads."""
    m49 = parse_m49(raw["unsd_m49"])
    reporters = _results(raw["comtrade_reporters"], "Comtrade Reporters.json")
    partners = _results(raw["comtrade_partners"], "Comtrade partnerAreas.json")
    wb_rows = parse_worldbank(raw["worldbank_countries"])

    by_iso3: dict[str, dict[str, str]] = {}
    for r in m49:
        iso3 = r["ISO-alpha3 Code"].strip()
        if not iso3:
            continue
        if iso3 in by_iso3:
            raise CrosswalkError(f"M49 overview lists ISO3 {iso3} twice")
        by_iso3[iso3] = r
    iso3_known = set(by_iso3)

    wb_economies = {r["id"]: r for r in wb_rows if ((r.get("region") or {}).get("value") or "").strip() != "Aggregates"}
    wb_aggregates = sorted(r["id"] for r in wb_rows if r["id"] not in wb_economies)

    unmapped: list[dict[str, Any]] = []

    def candidates(rows: list[dict[str, Any]], code_f: str, iso_f: str, note_f: str, provider: str,
                   ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            label = str(r.get("text") or "")
            iso = str(r.get(iso_f) or "").strip()
            flags = _code_flags(label=label, iso3_field=iso, is_group=bool(r.get("isGroup")),
                                effective=r.get("entryEffectiveDate"), expired=r.get("entryExpiredDate"),
                                iso3_known=iso3_known)
            if flags:
                unmapped.append({"provider": provider, "code": str(r[code_f]), "label": label,
                                 "provider_note": r.get(note_f), "provider_iso3_field": iso or None,
                                 "valid_from": r.get("entryEffectiveDate"), "valid_to": r.get("entryExpiredDate"),
                                 "flags": flags, "mapped_iso3": None})
                continue
            out[iso].append(r)
        for iso, rs in list(out.items()):
            if len(rs) > 1:
                for r in rs:
                    unmapped.append({"provider": provider, "code": str(r[code_f]), "label": r.get("text"),
                                     "provider_note": r.get(note_f), "provider_iso3_field": iso,
                                     "valid_from": r.get("entryEffectiveDate"), "valid_to": r.get("entryExpiredDate"),
                                     "flags": ["ambiguous_iso3"], "mapped_iso3": None})
                del out[iso]
        return out

    rep_by_iso = candidates(reporters, "reporterCode", "reporterCodeIsoAlpha3", "reporterNote", "comtrade_reporter")
    par_by_iso = candidates(partners, "PartnerCode", "PartnerCodeIsoAlpha3", "partnerNote", "comtrade_partner")

    keys = sorted(set(by_iso3) | set(wb_economies))
    entities: list[dict[str, Any]] = []
    for key in keys:
        m = by_iso3.get(key)
        wb = wb_economies.get(key)
        rep = (rep_by_iso.get(key) or [None])[0]
        par = (par_by_iso.get(key) or [None])[0]
        flags: list[str] = []
        if m is None:
            flags.append("not_iso3166")  # World Bank-specific economy code (CHI, XKX)
        if wb is None:
            flags.append("no_worldbank_economy")
        if rep is None:
            flags.append("no_comtrade_reporter")
        aliases: list[dict[str, str]] = []
        if m is not None:
            aliases.append({"provider": "iso3166_alpha3", "key": key})
            aliases.append({"provider": "un_m49", "key": str(int(m["M49 Code"]))})
        if wb is not None:
            aliases.append({"provider": "worldbank", "key": wb["id"]})
        entry: dict[str, Any] = {
            "key": key,
            "kind": "entity.country",
            "label": (m["Country or Area"] if m else wb["name"]).strip(),
            "iso3": key if m else None,
            "iso2": (m["ISO-alpha2 Code"] or None) if m else None,
            "m49": str(int(m["M49 Code"])) if m else None,
            "m49_region": (m.get("Region Name") or None) if m else None,
            "m49_subregion": (m.get("Sub-region Name") or None) if m else None,
            "worldbank": wb["id"] if wb else None,
            "worldbank_name": wb["name"].strip() if wb else None,
            "comtrade_reporter": None,
            "comtrade_partner": str(par["PartnerCode"]) if par else None,
            "reviewed_entry": key in reviewed_keys,
        }
        if rep is not None:
            code = str(rep["reporterCode"])
            note = rep.get("reporterNote")
            entry["comtrade_reporter"] = code
            entry["comtrade_reporter_label"] = rep.get("text")
            entry["comtrade_reporter_note"] = note
            aliases.append({"provider": "comtrade_reporter", "key": code})
            if m is not None and str(int(m["M49 Code"])) != code:
                flags.append("comtrade_code_differs_from_m49")
            label = str(rep.get("text") or "")
            if note and note != label and str(note).startswith(label + ", "):
                flags.append("comtrade_territory_note_lists_additional_areas")
        entry["flags"] = flags
        entry["aliases"] = aliases
        entities.append(entry)

    unmapped.sort(key=lambda u: (u["provider"], int(u["code"]) if str(u["code"]).isdigit() else 0, u["code"]))
    body = {"entities": entities, "unmapped_provider_codes": unmapped,
            "excluded_worldbank_aggregates": wb_aggregates}
    sources = [{"name": s.name, "file": s.file, "url": s.url, "sha256": _sha256(raw[s.name]),
                "archive_gzip": s.gzip, "retrieved_at": retrieved.get(s.name)} for s in SOURCES]
    stats = {
        "entities": len(entities),
        "iso3166_entities": sum(1 for e in entities if e["iso3"]),
        "worldbank_economies": sum(1 for e in entities if e["worldbank"]),
        "worldbank_aggregates_excluded": len(wb_aggregates),
        "comtrade_reporters_mapped": sum(1 for e in entities if e["comtrade_reporter"]),
        "comtrade_reporter_codes_unmapped": sum(1 for u in unmapped if u["provider"] == "comtrade_reporter"),
        "comtrade_partner_codes_unmapped": sum(1 for u in unmapped if u["provider"] == "comtrade_partner"),
        "with_worldbank_and_comtrade": sum(1 for e in entities if e["worldbank"] and e["comtrade_reporter"]),
    }
    return {
        "schema": SCHEMA,
        "version": CROSSWALK_VERSION,
        "review_status": "machine_derived_pending_maintainer_review",
        "built_by": "reva.adapters.connectors.country_crosswalk (V2 step 1, agent ALLC)",
        "window": {"from": WINDOW[0], "to": WINDOW[1]},
        "sources": sources,
        "rules": [
            "entity per ISO 3166 alpha-3 listed by UNSD M49, plus World Bank economies without one (not_iso3166)",
            "World Bank rows with region 'Aggregates' are excluded (regions, income and lending groups)",
            "a Comtrade code maps only if: not a group, not a 'nes' area, valid in the window, provider ISO3 "
            "field is an M49-listed ISO 3166 code, and unique for that ISO3; otherwise it is listed unmapped",
            "a reporter whose provider note lists more areas than its label is mapped but flagged "
            "comtrade_territory_note_lists_additional_areas",
            "partner codes are informational only; bilateral rows keep provider partner codes",
            "entities in countries.yaml keep their human-reviewed aliases (reviewed_entry)",
        ],
        "stats": stats,
        **body,
        "content_sha256": _sha256(_canonical(body)),
    }


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def _manifest_retrieved() -> dict[str, str]:
    manifest = yaml.safe_load((REFERENCE_DIR / "manifest.yaml").read_text(encoding="utf-8")) or []
    by_file = {m["file"]: m for m in manifest}
    out = {}
    for s in SOURCES:
        m = by_file.get(s.file)
        if m is None:
            raise CrosswalkError(f"registry/reference/manifest.yaml has no entry for {s.file}")
        out[s.name] = str(m.get("retrieved_at"))
    return out


def build_from_archive(root: Path = REFERENCE_DIR) -> dict[str, Any]:
    raw = {s.name: read_source(s, root) for s in SOURCES}
    return build(raw, retrieved=_manifest_retrieved(), reviewed_keys=_reviewed_keys())


# --------------------------------------------------------------------------- loading
def verify_sources(doc: dict[str, Any], root: Path = REFERENCE_DIR) -> None:
    """Each archived raw file must match the digest recorded in the crosswalk and the manifest."""
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8")) or []
    by_file = {m["file"]: m for m in manifest}
    for s in doc["sources"]:
        src = next((x for x in SOURCES if x.name == s["name"]), None)
        if src is None:
            raise CrosswalkError(f"unknown crosswalk source {s['name']!r}")
        got = _sha256(read_source(src, root))
        if got != s["sha256"]:
            raise CrosswalkError(f"archived {src.file} sha256 {got[:12]} != crosswalk {s['sha256'][:12]}")
        m = by_file.get(src.file)
        if m is None or m.get("sha256") != got:
            raise CrosswalkError(f"registry/reference/manifest.yaml digest for {src.file} does not match the archive")
    body = {k: doc[k] for k in ("entities", "unmapped_provider_codes", "excluded_worldbank_aggregates")}
    if _sha256(_canonical(body)) != doc.get("content_sha256"):
        raise CrosswalkError("crosswalk content_sha256 does not match its entities (edited by hand?)")


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime_ns: int) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise CrosswalkError(f"{path}: schema {doc.get('schema')!r} != {SCHEMA}")
    verify_sources(doc)
    return doc


def load_crosswalk(path: Path | None = None) -> dict[str, Any]:
    """The verified crosswalk document (raw archive digests and content digest checked)."""
    p = path or CROSSWALK_PATH
    return _load_cached(str(p), p.stat().st_mtime_ns)


def crosswalk_sha256(path: Path | None = None) -> str:
    return _sha256((path or CROSSWALK_PATH).read_bytes())


def comtrade_reporters(doc: dict[str, Any] | None = None) -> dict[str, int]:
    """Entity key -> mapped Comtrade reporter code (sorted by key)."""
    doc = doc or load_crosswalk()
    return {e["key"]: int(e["comtrade_reporter"]) for e in doc["entities"] if e.get("comtrade_reporter")}


def worldbank_economies(doc: dict[str, Any] | None = None) -> dict[str, str]:
    """Entity key -> World Bank economy code."""
    doc = doc or load_crosswalk()
    return {e["key"]: e["worldbank"] for e in doc["entities"] if e.get("worldbank")}


def seed_entities(doc: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Entities to seed from the crosswalk (those not already in countries.yaml)."""
    doc = doc or load_crosswalk()
    return [e for e in doc["entities"] if not e["reviewed_entry"]]


# --------------------------------------------------------------------------- CLI
async def _fetch_all() -> dict[str, tuple[bytes, str]]:
    from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
    from reva.adapters.http.safe_client import SafeHttpClient
    from reva.settings import get_settings

    settings = get_settings()
    client = SafeHttpClient(
        allowed_hosts=ALLOWED_HOSTS, timeout_seconds=60, max_bytes=8_000_000,
        contact_email=settings.reva_http_contact_email, proxy_mode=settings.reva_http_proxy_mode,
        rate_limiter=RateLimiter({h: HostPolicy(min_interval_seconds=3.0, max_concurrency=1) for h in ALLOWED_HOSTS}),
    )
    out = {}
    try:
        for s in SOURCES:
            r = await client.get(s.url)
            out[s.name] = (r.body, r.retrieved_at.astimezone(UTC).isoformat(timespec="seconds"))
    finally:
        await client.aclose()
    return out


def _write_archives(fetched: dict[str, tuple[bytes, str]]) -> None:
    manifest_path = REFERENCE_DIR / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or []
    by_file = {m["file"]: m for m in manifest}
    for s in SOURCES:
        body, retrieved = fetched[s.name]
        data = gzip.compress(body, mtime=0) if s.gzip else body
        (REFERENCE_DIR / s.file).write_bytes(data)
        entry = by_file.get(s.file) or {"file": s.file}
        if entry.get("sha256") != _sha256(body):  # same bytes: keep the first retrieval time
            entry.update({"url": s.url, "sha256": _sha256(body), "retrieved_at": retrieved})
        if s.gzip:
            entry["stored_as"] = "gzip (mtime 0) of the raw response bytes; sha256 is of the raw bytes"
        entry.setdefault("licence", {
            "unsd_m49": "United Nations (UNSD M49 standard; reference metadata)",
            "comtrade_reporters": "UN Comtrade terms of use (reference metadata)",
            "comtrade_partners": "UN Comtrade terms of use (reference metadata)",
            "worldbank_countries": "CC-BY-4.0 (World Bank API metadata)",
        }[s.name])
        entry.setdefault("use", "country crosswalk input (registry/entities/country_crosswalk.json)")
        if s.file not in by_file:
            manifest.append(entry)
            by_file[s.file] = entry
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    import asyncio

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "verify"
    if cmd == "fetch":
        _write_archives(asyncio.run(_fetch_all()))
        print("archived", ", ".join(s.file for s in SOURCES))
        return 0
    if cmd == "build":
        doc = build_from_archive()
        CROSSWALK_PATH.write_text(dumps(doc), encoding="utf-8")
        print(json.dumps(doc["stats"], indent=1))
        return 0
    if cmd == "verify":
        doc = load_crosswalk()
        rebuilt = build_from_archive()
        if rebuilt["content_sha256"] != doc["content_sha256"]:
            print("crosswalk differs from a rebuild of the archived sources", file=sys.stderr)
            return 1
        print(f"ok {datetime.now(UTC).date().isoformat()} content_sha256={doc['content_sha256']}")
        return 0
    print(f"unknown command {cmd!r}; use fetch | build | verify", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ALIAS_MAPPING_VERSION",
    "CROSSWALK_PATH",
    "CrosswalkError",
    "build",
    "build_from_archive",
    "comtrade_reporters",
    "crosswalk_sha256",
    "load_crosswalk",
    "parse_m49",
    "seed_entities",
    "verify_sources",
    "worldbank_economies",
]
