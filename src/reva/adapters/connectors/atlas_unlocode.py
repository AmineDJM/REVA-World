"""UN/LOCODE (United Nations Code for Trade and Transport Locations, UNECE) -> atlas counts + KG (agent V6INFRA).

Source ``atlas.unlocode`` (``source_key`` ``unece.unlocode``):

* the publications page ``https://unlocode.unece.org/publications/`` names the **official release** ("Latest
  Release ... Official biannual UN/LOCODE publication featuring all approved location codes", 2025-1 on
  2026-10-17) and links its package on the UN International Computing Centre GitLab
  (``opensource.unicc.org/un/unece/uncefact/vocab-locode/-/jobs/artifacts/<release>/download?job=package-release``).
  The release tag is read from that link on every run (never assumed); the continuously updated "Pre-Release" is
  not used (the page says it "may change before official release");
* the terms page ``https://unlocode.unece.org/terms``.

Rights (both statements archived and checked on every run, quoted verbatim; a change refuses the run):

* site footer: "All UN/CEFACT standards are free to use under CC By 4.0 license" (the home page presents
  UN/LOCODE as "A UN/CEFACT Global Standard");
* site terms: "The United Nations grants permission to Users to visit the Site and to download and copy the
  information, documents and materials (collectively, "Materials") from the Site for the User's personal,
  non-commercial use, without any right to resell or redistribute them or to compile or create derivative works
  therefrom".

The two statements conflict; REVA records ``licence_status: conflicting_statements`` and flags every row and KG
statement for maintainer review (owner decision in ``docs/dev/v6_infra.md``). Nothing is re-published.

Files read from the release ZIP: ``release/csv/UNLOCODE CodeListPart{1,2,3}.csv`` (columns, in order: Change,
Country, Location, Name, NameWoDiacritics, Subdivision, Function, Status, Date, IATA, Coordinates, Remarks),
``release/csv/SubdivisionCodes.csv`` and the SKOS vocabulary ``release/UNLOCODE.ttl`` (function code labels, quoted
verbatim, and the release identifier ``dct:identifier``).

Atlas table ``unlocode.locations`` (per ``Country``, ISO 3166-1 alpha-2, scheme ``iso2``; ``XZ`` "installations in
international waters" stays unmapped), period = retrieval day, ``flags.edition`` = the release:

* ``LOCATIONS``: distinct location codes of the country;
* ``LOCATIONS|function=<f>``: distinct codes whose Function field holds ``f`` (labels from the SKOS file).

Rows whose Location is empty are country headers (Name starting with ``.``) or reference entries (``=``): not
locations, counted in the report. A code listed on several rows (name variants, change ``#``) is one location: its
Name is the row without a change indicator (else the first), the other names are aliases; Function, Status,
Subdivision and Coordinates are asserted only when all its rows agree (otherwise ``conflicting_rows``, not
asserted).

KG (source key ``unece.unlocode``): one entity ``unlocode:<CC><LLL>`` (kind ``trade_location``) per location whose
Function includes ``1`` (maritime port) or ``4`` (airport) -- a size bound, not a judgement (the other functions
are in the atlas counts). Statements: ``P17`` country, ``P625`` coordinates (the file's degrees and minutes, converted
to decimal degrees: ``4230N 00131E`` -> 42.5, 1.516667), ``unlocode:function`` (raw code + labels),
``unlocode:status`` (raw code), ``unlocode:subdivision_code`` (published code, literal), ``unlocode:iata`` and
``unlocode:in_adm1`` -> ``iso3166_2:<CC>-<Subdivision>`` (basis ``published_code``) when the published
subdivision names a Natural Earth admin-1 subdivision, else by REVA point in polygon (basis ``point_in_polygon``;
for maritime ports offshore of the generalised coastline, the unique nearest polygon within 0.05 degrees, basis
``nearest_polygon``), see :mod:`v6infra_common`.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from reva.adapters.connectors import atlas_infra_common as ic
from reva.adapters.connectors import v6infra_common as vc
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.unlocode"
VERSION = "1.0.1"  # 1.0.1: digest changed with atlas_ghcnm (FIX4C); Natural Earth archived under its own spec
SOURCE_KEY = "unece.unlocode"
ATLAS_SOURCE = "unlocode"
DATASET = "locations"
PARSER_VERSION = "atlas_unlocode/1"
SITE = "https://unlocode.unece.org"
PUBLICATIONS_URL = f"{SITE}/publications/"
TERMS_URL = f"{SITE}/terms"
GITLAB_HOST = "opensource.unicc.org"
RELEASE_LINK_RE = re.compile(
    r"https://opensource\.unicc\.org/un/unece/uncefact/vocab-locode/-/jobs/artifacts/(\d{4}-\d)/download\?job=package-release"
)
FOOTER_MARKER = "All UN/CEFACT standards are free to use under CC By 4.0 license"
TERMS_MARKER = (
    "for the User’s personal, non-commercial use, without any right to resell or redistribute them or to compile or "
    "create derivative works therefrom"
)
RELEASE_MARKER = "Official biannual UN/LOCODE publication featuring all approved location codes"
COLUMNS = ("Change", "Country", "Location", "Name", "NameWoDiacritics", "Subdivision", "Function", "Status", "Date",
           "IATA", "Coordinates", "Remarks")  # fmt: skip
FUNCTION_RE = re.compile(r"^[0-9AB-]{8}$")
KG_FUNCTIONS = ("1", "4")
COASTAL_DEG = 0.05  # maritime ports: nearest admin-1 polygon within 0.05 degrees when offshore (labelled)
MIN_LOCATIONS = 100_000  # truncation guard (116,232 in 2025-1; trimmed fixtures lower it)
MAX_BYTES = 40_000_000
RIGHTS: dict[str, Any] = {
    "licence": "conflicting statements: site footer 'All UN/CEFACT standards are free to use under CC By 4.0 "
    "license'; site terms: personal, non-commercial use, no redistribution, no derivative works",
    "licence_status": "conflicting_statements",
    "open_licence": None,
    "non_commercial": "per site terms (https://unlocode.unece.org/terms)",
    "redistribution": "not allowed per site terms; allowed under CC BY 4.0 per site footer -- maintainer review",
    "licence_review": "pending owner decision (docs/dev/v6_infra.md)",
    "attribution": "UN/LOCODE, United Nations Economic Commission for Europe (UNECE), https://unlocode.unece.org",
    "terms_url": TERMS_URL,
}
FLAG_RIGHTS = {"licence_status": "conflicting_statements", "licence_review": "pending"}

KG_PREDICATES: dict[str, tuple[str, str, str, str]] = {
    "unlocode:function": ("string", "unlocode_function", "UN/LOCODE function code", "code de fonction UN/LOCODE"),
    "unlocode:status": ("string", "unlocode_status", "UN/LOCODE status code", "code de statut UN/LOCODE"),
    "unlocode:subdivision_code": (
        "string",
        "unlocode_subdivision_code",
        "subdivision code published by UN/LOCODE",
        "code de subdivision publié par UN/LOCODE",
    ),
    "unlocode:iata": ("external_id", "unlocode_iata", "IATA code (UN/LOCODE column)", "code IATA (colonne UN/LOCODE)"),
    "unlocode:in_adm1": (
        "item",
        "unlocode_in_adm1",
        "located in first-level subdivision",
        "situé dans la subdivision de premier niveau",
    ),
}
WIKIDATA_PIDS = ("P17", "P625")


def _err(msg: str, kind: str = "schema_drift") -> SourceResponseError:
    return SourceResponseError(msg, retryable=False, kind=kind)


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__], vc, vc.gh)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="UNECE UN/LOCODE (official release package on opensource.unicc.org); Natural Earth admin-1",
        licence=RIGHTS["licence"] + "; flagged for maintainer review",
        allowed_hosts=("unlocode.unece.org", GITLAB_HOST, vc.NE_HOST),
        datasets=(DATASET,),
        dataset_ids=("unece:unlocode:release",),
        cadence_seconds=180 * ic.DAY,
        planned_requests=5,  # publications page, terms page, release package (+1 redirect hop), Natural Earth
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        notes="Official UN/LOCODE release: location counts per country and function; ports and airports as KG "
        "trade_location entities (country, coordinates, function, ADM1). Licence statements conflict: flagged.",
    )


# --------------------------------------------------------------------------- parsing
def check_pages(publications: bytes, terms: bytes) -> str:
    """Licence markers + the official release tag named by the publications page."""
    pub = re.sub(r"\s+", " ", publications.decode("utf-8", "replace"))
    trm = re.sub(r"\s+", " ", terms.decode("utf-8", "replace")).replace("&rsquo;", "’").replace("&#8217;", "’")
    if FOOTER_MARKER not in pub:
        raise _err("UN/LOCODE publications page no longer states the CC BY 4.0 footer", kind="licence")
    if TERMS_MARKER not in trm and TERMS_MARKER.replace("’", "'") not in trm:
        raise _err("UN/LOCODE terms page changed (non-commercial clause not found)", kind="licence")
    if RELEASE_MARKER not in pub:
        raise _err("UN/LOCODE publications page no longer describes the official release")
    tags = sorted(set(RELEASE_LINK_RE.findall(pub)))
    if len(tags) != 1:
        raise _err(f"UN/LOCODE publications page links {len(tags)} official release packages: {tags}")
    return str(tags[0])


def release_url(tag: str) -> str:
    return f"https://{GITLAB_HOST}/un/unece/uncefact/vocab-locode/-/jobs/artifacts/{tag}/download?job=package-release"


@dataclass
class Location:
    code: str
    country: str
    rows: list[list[str]] = field(default_factory=list)

    def primary(self) -> list[str]:
        plain = [r for r in self.rows if not r[0].strip()]
        return (plain or self.rows)[0]

    def agreed(self, col: int) -> str | None:
        """The column's value when every row agrees (stripped), else None (conflict)."""
        vals = {r[col].strip() for r in self.rows}
        return next(iter(vals)) if len(vals) == 1 else None

    def functions(self) -> str | None:
        return self.agreed(6)


@dataclass
class Release:
    tag: str
    identifier: str
    locations: dict[str, Location]
    function_labels: dict[str, str]
    subdivisions: dict[str, tuple[str, str]]  # "AD-02" -> (name, type)
    report: dict[str, Any]


def _ttl_functions(ttl: str) -> tuple[dict[str, str], str]:
    labels = dict(re.findall(r'unlcdf:([0-9A-Z]) a skos:Concept ;\s*skos:prefLabel "([^"]+)"@en', ttl))
    ident = re.search(r'<https://vocabulary\.uncefact\.org/unlocode> a skos:ConceptScheme ;.*?dct:identifier "([^"]+)"',
                      ttl, flags=re.S)  # fmt: skip
    if not labels or ident is None:
        raise _err("UN/LOCODE SKOS file lacks the function code scheme or the release identifier")
    return labels, ident.group(1)


def read_release(body: bytes, tag: str, *, min_locations: int | None = None) -> Release:
    try:
        z = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise _err(f"UN/LOCODE release package is not a ZIP ({e})") from e
    names = z.namelist()
    parts = sorted(n for n in names if re.match(r"^release/csv/UNLOCODE CodeListPart\d\.csv$", n))
    if not parts or "release/csv/SubdivisionCodes.csv" not in names or "release/UNLOCODE.ttl" not in names:
        raise _err(f"UN/LOCODE release package lacks expected members: {names[:20]}")
    labels, ident = _ttl_functions(z.read("release/UNLOCODE.ttl").decode("utf-8"))
    if ident != tag:
        raise _err(f"release package identifies itself as {ident}, the publications page links {tag}")
    rep: Counter[str] = Counter()
    locs: dict[str, Location] = {}
    for p in parts:
        for i, r in enumerate(csv.reader(io.StringIO(z.read(p).decode("utf-8")))):
            if len(r) != len(COLUMNS):
                raise _err(f"{p} row {i}: {len(r)} fields, expected {len(COLUMNS)}")
            if not r[2].strip():
                rep["country_header_rows" if r[3].startswith(".") else "reference_rows_without_location"] += 1
                continue
            if not FUNCTION_RE.match(r[6]):
                raise _err(f"{p} row {i}: Function {r[6]!r} is not an 8-character function code")
            code = r[1].strip() + r[2].strip()
            if not re.match(r"^[A-Z]{2}[A-Z0-9]{3}$", code):
                raise _err(f"{p} row {i}: malformed code {code!r}")
            if not re.match(r"^[A-Z]{2}[A-Z2-9]{3}$", code):
                rep["codes_with_digit_0_or_1"] += 1  # outside Rec 16's letters + digits 2-9; kept as published
            locs.setdefault(code, Location(code, r[1].strip())).rows.append(r)
            rep["location_rows"] += 1
    unknown = sorted({ch for loc in locs.values() for r in loc.rows for ch in r[6] if ch != "-" and ch not in labels})
    if unknown:
        raise _err(f"function characters without a SKOS label: {unknown}")
    floor = MIN_LOCATIONS if min_locations is None else min_locations
    if len(locs) < floor:
        raise _err(f"UN/LOCODE release lists only {len(locs)} locations (truncated?)", kind="incomplete")
    subs: dict[str, tuple[str, str]] = {}
    for r in csv.reader(io.StringIO(z.read("release/csv/SubdivisionCodes.csv").decode("utf-8"))):
        if len(r) >= 3 and r[0].strip() and r[1].strip():
            subs[f"{r[0].strip()}-{r[1].strip()}"] = (r[2].strip(), (r[3].strip() if len(r) > 3 else ""))
    rep["locations"] = len(locs)
    rep["codes_on_several_rows"] = sum(1 for v in locs.values() if len(v.rows) > 1)
    return Release(tag, ident, locs, labels, subs, dict(rep))


def build_atlas(
    rel: Release, raw: ic.Fetched, terms: ic.Fetched, pub: ic.Fetched
) -> tuple[ic.RowBuilder, dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    total: Counter[str] = Counter()
    by_fn: Counter[tuple[str, str]] = Counter()
    conflicts = 0
    for loc in rel.locations.values():
        total[loc.country] += 1
        fn = loc.functions()
        if fn is None:  # the rows of the code disagree on Function: counted in LOCATIONS only
            conflicts += 1
            continue
        for ch in sorted(set(fn) - {"-"}):
            by_fn[(loc.country, ch)] += 1
    period = ic.day_span(raw.retrieved_at.date())
    rb = ic.RowBuilder(ATLAS_SOURCE, DATASET)
    base = {**FLAG_RIGHTS, "edition": rel.tag, "edition_kind": "official release"}
    for c in sorted(total):
        iso3 = country_iso3(c, "iso2")

        def emit(ind: str, v: int, formula: str, where: dict[str, Any], extra: dict[str, Any], c: str = c,
                 iso3: Any = iso3) -> None:  # fmt: skip
            rb.count(indicator=ind, provider_geo=c, iso3=iso3, period=period, value=v, unit="locations",
                     raw_sha256=raw.sha256, selector={"file": "release/csv/UNLOCODE CodeListPart*.csv",
                                                      "aggregate": "count distinct Country+Location", "where": where},
                     flags=ic.derived(formula, inputs=[terms, pub], **base, **extra))  # fmt: skip

        emit("LOCATIONS", total[c], "count of distinct location codes (Country + Location) with Country = the code",
             {"Country": c}, {})  # fmt: skip
        for ch, label in sorted(rel.function_labels.items()):
            emit(f"LOCATIONS|function={ch}", by_fn.get((c, ch), 0),
                 f"count of distinct location codes whose Function field contains '{ch}' (all rows of the code agree)",
                 {"Country": c, "Function_contains": ch}, {"function_label": label})  # fmt: skip
    report = {**rel.report, "countries": len(total), "function_conflicts_not_counted": conflicts,
              "unmapped": sorted(c for c in total if country_iso3(c, "iso2") is None)}  # fmt: skip
    return rb, report


def build_kg(rel: Release, raw: ic.Fetched, idx: vc.Adm1Index | None, qids: dict[str, str]) -> vc.KgBuilder:
    from reva.atlas.tables import country_iso3

    kb = vc.KgBuilder(SOURCE_KEY, f"unlocode-{rel.tag}")
    ref = [vc.kg_ref(raw, file="release/csv/UNLOCODE CodeListPart*.csv", licence=RIGHTS["licence"],
                     edition=rel.tag, licence_status="conflicting_statements")]  # fmt: skip
    adm_ref = [*ref, idx.ref()] if idx is not None else ref
    for code, loc in sorted(rel.locations.items()):
        fn = loc.functions()
        if fn is None:
            kb.counts["skipped_function_conflict"] += 1
            continue
        if not any(f in fn for f in KG_FUNCTIONS):
            continue
        prim = loc.primary()
        qid = f"unlocode:{code}"
        names = [r[3].strip() for r in loc.rows] + [r[4].strip() for r in loc.rows]
        fl = [rel.function_labels[ch] for ch in fn if ch != "-"]
        kb.entity(qid, "trade_location", prim[3].strip() or code, aliases=[*names, f"{code[:2]} {code[2:]}", code],
                  description=f"UN/LOCODE {code[:2]} {code[2:]} ({'; '.join(fl)}), release {rel.tag}")  # fmt: skip
        q0 = {"edition": rel.tag, "rows": len(loc.rows)}

        def st(pid: str, *, obj: str | None = None, value: dict[str, Any] | None = None,
               quals: dict[str, Any] | None = None, refs: list[dict[str, Any]] = ref, qid: str = qid, q0: dict[str, Any] = q0) -> None:  # fmt: skip
            kb.st(qid, pid, raw=raw.sha256, refs=refs, obj=obj, value=value, quals={**q0, **(quals or {})})

        iso3 = country_iso3(loc.country, "iso2")
        cq = vc.country_qid(iso3, qids)
        coords = vc.locode_coordinates(loc.agreed(10) or "")
        sub = loc.agreed(5)
        place = vc.place_facility(idx, country_iso2=loc.country, published_code=f"{loc.country}-{sub}" if sub else None,
                                  lat=coords[0] if coords else None, lon=coords[1] if coords else None,
                                  coastal_deg=COASTAL_DEG if "1" in fn else None)  # fmt: skip
        if cq:
            pq: dict[str, Any] = {"basis": "UN/LOCODE Country column (ISO 3166-1 alpha-2)", "country_code": loc.country}
            if "coordinates_fall_in" in place.quals:
                pq["coordinates_fall_in"] = place.quals["coordinates_fall_in"]
            st("P17", obj=cq, quals=pq)
        else:
            kb.counts["country_unmapped_or_no_qid"] += 1
        if coords:
            st("P625", value=vc.coordinate_value(*coords),
               quals={"provider_text": loc.agreed(10), "conversion": "degrees + minutes/60 (UN/LOCODE DDMM[NS] DDDMM[EW])"})  # fmt: skip
        elif loc.agreed(10) is None:
            kb.counts["coordinates_conflict"] += 1
        st("unlocode:function", value={"type": "string", "value": fn}, quals={"functions": fl})
        status = loc.agreed(7)
        if status:
            st("unlocode:status", value={"type": "string", "value": status})
        if sub:
            sq = {"subdivision_name": rel.subdivisions.get(f"{loc.country}-{sub}", ("", ""))[0] or None}
            st("unlocode:subdivision_code", value={"type": "string", "value": sub}, quals=sq)
        iata = loc.agreed(9)
        if iata:
            st("unlocode:iata", value={"type": "external_id", "value": iata})
        if place.code:
            st("unlocode:in_adm1", obj=f"iso3166_2:{place.code}", quals=place.quals, refs=adm_ref)
        kb.counts[f"adm1_{place.reason}"] += 1
    return kb


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    if not run.wants(DATASET):
        return {"ok": True, "tables": [], "skipped": DATASET}
    pub = await ic.fetch_archived(run, PUBLICATIONS_URL, name="publications.html", key="unlocode:publications",
                                  media_types=("text/html",), max_bytes=2_000_000)  # fmt: skip
    terms = await ic.fetch_archived(run, TERMS_URL, name="terms.html", key="unlocode:terms", media_types=("text/html",),
                                    max_bytes=2_000_000)  # fmt: skip
    tag = check_pages(pub.body, terms.body)
    raw = await ic.fetch_archived(run, release_url(tag), name=f"unlocode-{tag}.zip", key="unlocode:release",
                                  media_types=("application/zip", "application/octet-stream"), max_bytes=MAX_BYTES)  # fmt: skip
    rel = read_release(raw.body, tag)
    rb, report = build_atlas(rel, raw, terms, pub)
    out = run.register(
        dataset=DATASET,
        frame=rb.frame(),
        raw_sha256s=[pub.sha256, terms.sha256, raw.sha256],
        coverage_note=f"UN/LOCODE official release {tag}: {report['locations']} location codes in {report['countries']} "
        f"countries; unmapped: {', '.join(report['unmapped']) or 'none'}. LICENCE STATEMENTS CONFLICT (CC BY 4.0 "
        "footer vs non-commercial site terms): flagged for maintainer review",
        geo_scheme="iso2",
        rights={**RIGHTS, "source_key": SOURCE_KEY},
    )
    idx = await vc.fetch_ne_admin1(run)
    kb = build_kg(rel, raw, idx, load_country_qids())
    kg = vc.load(run, kb, KG_PREDICATES, WIKIDATA_PIDS, [raw.sha256, idx.fetched.sha256], raw.retrieved_at,
                 {"connector": NAME, "parser": PARSER_VERSION, "edition": tag})  # fmt: skip
    kg["counts"] = dict(kb.counts)
    table = {"table": out["dataset_key"].removeprefix("atlas."), "rows": out["row_count"], "created": out["created"],
             "artifact_sha256": out["artifact_sha256"],
             "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []]}  # fmt: skip
    return {"ok": True, "tables": [table], "report": report, "edition": tag, "kg": kg}
