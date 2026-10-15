"""Shared helpers for the geopolitics connectors (agent KGGEOPOL): UCDP, UNGA votes, CShapes.

* :class:`StateCodeCrosswalk` -- Gleditsch-Ward (GW) / Correlates-of-War (COW) state codes + a date
  -> crosswalk entity key (ISO 3166 alpha-3, or ``XKX``). Built from the ``countrycode`` R package's
  ``codelist_panel`` (country-year panel; CRAN tarball pinned by sha256, read with
  :mod:`reva.adapters.connectors.atlas_rdata`) plus the explicit, documented
  :data:`STATE_CODE_POLICY`: inside a policy window the window decides (a historical state with no
  current entity resolves to ``None`` with its name; a documented continuation resolves to its ISO3);
  outside every window the panel decides. Codes that resolve to nothing are kept by the callers
  (``iso3 = null``, provider code verbatim) and listed -- never dropped, never guessed.
* KG statement / entity records in a neutral form (:class:`KgStatement`, :class:`KgEntity`) that the
  loader adapts to ``reva.kg`` (agent KGCORE).
* small parsing helpers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

# --------------------------------------------------------------------------------------
# GW / COW -> crosswalk entity key
# --------------------------------------------------------------------------------------

_OPEN = date(1800, 1, 1)
_FUTURE = date(2100, 1, 1)

#: (code_system, code) -> [(start, end) inclusive, iso3 or None, label, note]. Sources: Gleditsch & Ward
#: (1999) independent-states list and its date conventions (http://ksgleditsch.com/data-4.html), COW
#: State System Membership v2016, ISO 3166-1/-3. A ``None`` iso3 means "this code denotes a state with
#: no current crosswalk entity at these dates" (Soviet Union, West/East Germany, Yugoslavia,
#: Czechoslovakia, North/South Yemen, North/South Vietnam, Zanzibar, German Reich).
STATE_CODE_POLICY: dict[tuple[str, int], list[tuple[date, date, str | None, str, str]]] = {}


def _p(systems: Iterable[str], code: int, start: date, end: date, iso3: str | None, label: str, note: str) -> None:
    for s in systems:
        STATE_CODE_POLICY.setdefault((s, code), []).append((start, end, iso3, label, note))


_BOTH = ("gw", "cow")
_p(
    _BOTH,
    365,
    date(1922, 12, 30),
    date(1991, 12, 20),
    None,
    "Soviet Union (USSR)",
    "GW/COW use 365 for Russia throughout ('Russia (Soviet Union)'); the USSR period is not the ISO entity RUS",
)
_p(
    ("gw",),
    260,
    date(1949, 9, 21),
    date(1990, 10, 2),
    None,
    "German Federal Republic (West Germany)",
    "GW 260 = FRG until unification, then unified Germany (DEU)",
)
_p(
    ("cow",),
    260,
    _OPEN,
    date(1990, 10, 2),
    None,
    "German Federal Republic (West Germany)",
    "COW 260 = FRG 1955-1990; unified Germany is COW 255 from 1990-10-03",
)
_p(
    ("cow",),
    260,
    date(1990, 10, 3),
    _FUTURE,
    None,
    "German Federal Republic (COW 260 after unification)",
    "COW codes unified Germany 255; a 260 row after 1990-10-02 is not mapped (would duplicate DEU)",
)
_p(
    _BOTH,
    265,
    _OPEN,
    _FUTURE,
    None,
    "German Democratic Republic (East Germany)",
    "no current ISO entity (ISO 3166-3 DDDE)",
)
_p(
    _BOTH,
    255,
    _OPEN,
    date(1945, 5, 8),
    None,
    "Germany (Prussia / German Reich)",
    "pre-1945 German state (GW: 'Germany (Prussia)')",
)
_p(
    _BOTH,
    345,
    _OPEN,
    date(2006, 6, 4),
    None,
    "Yugoslavia / Serbia and Montenegro",
    "Yugoslavia (SFRY, FRY) and Serbia and Montenegro until Montenegro's independence (2006-06-03/05)",
)
_p(
    ("cow",),
    345,
    date(2006, 6, 5),
    _FUTURE,
    "SRB",
    "Serbia",
    "COW continues code 345 for Serbia after 2006-06-05 (GW uses 340)",
)
_p(_BOTH, 315, _OPEN, _FUTURE, None, "Czechoslovakia", "no current ISO entity (ISO 3166-3 CSHH); Czechia is 316")
_p(_BOTH, 678, _OPEN, date(1990, 5, 21), None, "Yemen Arab Republic (North Yemen)", "before unification 1990-05-22")
_p(("gw",), 678, date(1990, 5, 22), _FUTURE, "YEM", "Yemen", "GW continues code 678 for unified Yemen (COW uses 679)")
_p(
    _BOTH,
    680,
    _OPEN,
    _FUTURE,
    None,
    "People's Democratic Republic of Yemen (South Yemen)",
    "merged into Yemen 1990-05-22",
)
_p(
    _BOTH,
    816,
    _OPEN,
    date(1976, 7, 1),
    None,
    "Democratic Republic of Vietnam (North Vietnam)",
    "before reunification 1976-07-02",
)
_p(_BOTH, 816, date(1976, 7, 2), _FUTURE, "VNM", "Vietnam", "GW/COW 816 = unified Vietnam from 1976-07-02")
_p(_BOTH, 817, _OPEN, _FUTURE, None, "Republic of Vietnam (South Vietnam)", "no current ISO entity")
_p(_BOTH, 511, _OPEN, _FUTURE, None, "Zanzibar", "independent 1963-12-19 to 1964-04-26; no current ISO entity")
_p(
    _BOTH,
    347,
    date(2008, 2, 17),
    _FUTURE,
    "XKX",
    "Kosovo",
    "crosswalk entity XKX is the World Bank code for Kosovo (not ISO 3166)",
)

# UCDP uses GW codes but keeps 345 ("Serbia (Yugoslavia)") for Serbia after 2006 instead of GW 340
# (UCDP Country-Year Dataset 26.1 rows 2006-2025 for country_id 345).
_p(("ucdp",), 345, date(2006, 6, 5), _FUTURE, "SRB", "Serbia", "UCDP keeps GW 345 for Serbia after 2006")
_p(("gw",), 751, _OPEN, _FUTURE, None, "Hyderabad", "princely state, independent 1947-1948 (GW 751)")
_p(("gw",), 396, _OPEN, _FUTURE, None, "Abkhazia", "GW microstates list; not a crosswalk entity")
_p(("gw",), 397, _OPEN, _FUTURE, None, "South Ossetia", "GW microstates list; not a crosswalk entity")

#: Gleditsch-Ward *microstates* (http://ksgleditsch.com/data/microstatessystem.dat, retrieved 2026-10-15,
#: sha256 5a220717ca4fa27b69c4ef0670f9897438999ef5a08ee5e18dda84574e04b629): countrycode's panel has no GW
#: number for them. Code -> (GW name as listed, ISO3). NB GW 970-973 differ from COW (COW 970 = Nauru).
GW_MICROSTATES: dict[int, tuple[str, str]] = {
    54: ("Dominica", "DMA"),
    55: ("Grenada", "GRD"),
    56: ("Saint Lucia", "LCA"),
    57: ("Saint Vincent and the Grenadines", "VCT"),
    58: ("Antigua & Barbuda", "ATG"),
    60: ("Saint Kitts and Nevis", "KNA"),
    221: ("Monaco", "MCO"),
    223: ("Liechtenstein", "LIE"),
    331: ("San Marino", "SMR"),
    232: ("Andorra", "AND"),
    403: ("Sao Tome and Principe", "STP"),
    591: ("Seychelles", "SYC"),
    935: ("Vanuatu", "VUT"),
    970: ("Kiribati", "KIR"),
    971: ("Nauru", "NRU"),
    972: ("Tonga", "TON"),
    973: ("Tuvalu", "TUV"),
    983: ("Marshall Islands", "MHL"),
    986: ("Palau", "PLW"),
    987: ("Federated States of Micronesia", "FSM"),
    990: ("Samoa/Western Samoa", "WSM"),
}
for _code, (_name, _iso) in GW_MICROSTATES.items():
    _p(("gw",), _code, _OPEN, _FUTURE, _iso, _name, "Gleditsch-Ward microstates list")


@dataclass(frozen=True)
class CodeResolution:
    code_system: str
    code: int
    iso3: str | None
    label: str | None
    reason: str  # historical_qid | policy | panel | panel_nearest_year | unmapped
    qid: str | None = None  # Wikidata QID of a historical state (registry/entities/historical_states_qids.json)


class StateCodeCrosswalk:
    """GW/COW code + date -> crosswalk entity key (see module docstring)."""

    def __init__(
        self,
        panel_rows: Iterable[Mapping[str, Any]],
        *,
        source_sha256: str,
        known_keys: frozenset[str] | None = None,
        historical: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.source_sha256 = source_sha256
        self.known_keys = known_keys
        self.historical = list(load_historical_qids() if historical is None else historical)
        self._by: dict[tuple[str, int], dict[int, tuple[str | None, str | None]]] = {}
        for r in panel_rows:
            year = r.get("year")
            if year is None:
                continue
            iso3 = r.get("iso3c") or None
            name = r.get("country.name.en")
            for system, col in (("gw", "gwn"), ("cow", "cown")):
                code = r.get(col)
                if code is None:
                    continue
                self._by.setdefault((system, int(float(code))), {})[int(float(year))] = (iso3, name)

    @classmethod
    def from_countrycode_rda(cls, blob: bytes, *, known_keys: frozenset[str] | None = None) -> StateCodeCrosswalk:
        from reva.adapters.connectors.atlas_rdata import column_text, read_rdata_frame

        cols = ("country.name.en", "year", "iso3c", "gwn", "cown")
        frame = read_rdata_frame(blob, "codelist_panel", cols)
        rows = ({c: column_text(frame.columns[c], i) for c in cols} for i in range(frame.nrows))
        return cls(rows, source_sha256=hashlib.sha256(blob).hexdigest(), known_keys=known_keys)

    def _known(self, iso3: str | None) -> str | None:
        if iso3 is None or self.known_keys is None:
            return iso3
        return iso3 if iso3 in self.known_keys else None

    def resolve(self, system: str, code: int | None, when: date) -> CodeResolution:
        if code is None:
            return CodeResolution(system, -1, None, None, "unmapped")
        code = int(code)
        # 1. a policy window that names a current entity (documented continuation, e.g. GW 678 -> YEM after
        #    1990-05-22, COW 345 -> SRB after 2006-06-05) ; 2. verified historical-state QIDs (dated);
        #    3. a policy window for a historical state without QID; 4. the countrycode panel.
        # "ucdp" = GW codes with UCDP's own deviations (its policy entries win, then GW's).
        systems = ("ucdp", "gw") if system == "ucdp" else (system,)
        window = None
        for sys_ in systems:
            for start, end, iso3, label, _note in STATE_CODE_POLICY.get((sys_, code), []):
                if window is None and start <= when <= end:
                    window = (iso3, label)
        if window is not None and window[0] is not None:
            return CodeResolution(system, code, self._known(window[0]), window[1], "policy")
        use_hist = system == "cow" or code in GW_SHARES_COW_HISTORICAL
        for h in self.historical if use_hist else ():
            if (
                h["code"] == code
                and (h["from"] is None or h["from"] <= when.year)
                and (h["to"] is None or when.year <= h["to"])
            ):
                return CodeResolution(system, code, None, h["label"], "historical_qid", h["qid"])
        if window is not None:
            return CodeResolution(system, code, None, window[1], "policy")
        system_panel = "gw" if system == "ucdp" else system
        years = self._by.get((system_panel, code))
        if not years:
            return CodeResolution(system, code, None, None, "unmapped")
        if when.year in years:
            iso3, name = years[when.year]
            iso3 = self._known(iso3)
            return CodeResolution(system, code, iso3, name, "panel" if iso3 else "unmapped")
        # Outside the panel's years (it ends before the latest data year): use the nearest panel year
        # only when the code maps to exactly one entity across all panel years (stable continuation).
        isos = {v[0] for v in years.values() if v[0]}
        if len(isos) == 1:
            nearest = min(years, key=lambda y: abs(y - when.year))
            iso3, name = years[nearest]
            iso3 = self._known(iso3)
            if iso3:
                return CodeResolution(system, code, iso3, name, "panel_nearest_year")
        return CodeResolution(system, code, None, None, "unmapped")


#: COW numbers of historical states in registry/entities/historical_states_qids.json that also denote the
#: same polity under the GW coding (GW and COW agree on these codes for the listed years).
GW_SHARES_COW_HISTORICAL = frozenset(
    {255, 260, 265, 300, 315, 325, 345, 365, 511, 640, 678, 680, 710, 713, 730, 816, 817}
)


def load_historical_qids(system: str | None = None) -> list[dict[str, Any]]:
    """Dated (code -> QID) entries of A-HISTECON's ``historical_states_qids.json`` (provider ``cow``), each
    checked against Wikidata with evidence (agent-mapped, pending maintainer review). Empty when absent."""
    from reva.ontology.registry import REGISTRY_ROOT

    path = REGISTRY_ROOT / "entities" / "historical_states_qids.json"
    if not path.exists():
        return []
    out = []
    for e in json.loads(path.read_text()).get("entries", []):
        if e.get("provider") != "cow" or not str(e.get("code", "")).isdigit():
            continue
        out.append(
            {
                "code": int(e["code"]),
                "from": e.get("valid_from_year"),
                "to": e.get("valid_to_year"),
                "qid": e["qid"],
                "label": e.get("wikidata_label_en") or e.get("provider_label"),
            }
        )
    return out


def crosswalk_keys() -> frozenset[str]:
    """Entity keys of ``registry/entities/country_crosswalk.json`` (the atlas country list)."""
    from reva.atlas.tables import known_country_keys

    return known_country_keys()


# --------------------------------------------------------------------------------------
# Neutral KG records (adapted to reva.kg by the loader)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class KgEntity:
    """A dataset-native entity (``<ns>:<key>``, e.g. ``ucdp:conflict:200``) or a Wikidata QID."""

    qid: str
    kind: str
    labels: Mapping[str, str]
    descriptions: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class KgStatement:
    """One sourced statement. ``property`` is a dataset predicate (``ucdp:side_in_conflict``) or a
    Wikidata PID. Exactly one of ``object_qid`` / ``value``. Times are ISO dates (``YYYY-MM-DD``) with
    Wikibase precision (11 = day, 10 = month, 9 = year)."""

    key: str  # unique within the source: becomes "<source_key>:<key>"
    subject: str
    property: str
    object_qid: str | None = None
    value: Mapping[str, Any] | None = None
    start: str | None = None
    start_precision: int | None = None
    end: str | None = None
    end_precision: int | None = None
    point_in_time: str | None = None
    pit_precision: int | None = None
    qualifiers: Mapping[str, Any] = field(default_factory=dict)
    raw_sha256: str = ""
    selector: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.object_qid is None) == (self.value is None):
            raise ValueError("exactly one of object_qid / value")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


# --------------------------------------------------------------------------------------
# small parsing helpers
# --------------------------------------------------------------------------------------


def int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.upper() in ("NA", "NAN", "NONE"):
        return None
    return int(float(s)) if any(c in s for c in ".eE") else int(s)


def iso_date_or_none(v: Any) -> date | None:
    s = str(v).strip() if v is not None else ""
    if not s or s.upper() == "NA":
        return None
    return date.fromisoformat(s[:10])


# --------------------------------------------------------------------------------------
# State entities for KG statements
# --------------------------------------------------------------------------------------


class StateEntityResolver:
    """(code system, code, date) -> KG entity id.

    A code that resolves to a crosswalk entity with a known Wikidata QID becomes that QID; anything
    else becomes the dataset-native state-code entity ``gw:<code>`` / ``cow:<code>`` (labelled with the
    provider's own name). Correspondence statements ``geopol:state_code_of`` (code entity -> country
    QID, dated) are emitted by :func:`code_correspondence_statements` so the code entities stay linked.
    """

    def __init__(self, crosswalk: StateCodeCrosswalk, qid_for_iso3: Mapping[str, str]) -> None:
        self.crosswalk = crosswalk
        self.qid_for_iso3 = dict(qid_for_iso3)
        self.code_labels: dict[str, str] = {}
        self.unresolved: dict[str, set[int]] = {}

    def entity(self, system: str, code: int, when: date, provider_label: str | None = None) -> str:
        r = self.crosswalk.resolve(system, code, when)
        if r.qid:
            return r.qid
        if r.iso3 and r.iso3 in self.qid_for_iso3:
            return self.qid_for_iso3[r.iso3]
        eid = f"{'gw' if system == 'ucdp' else system}:{code}"
        label = r.label or provider_label
        if label and eid not in self.code_labels:
            self.code_labels[eid] = label
        self.unresolved.setdefault(eid, set()).add(when.year)
        return eid

    def code_entities(self) -> list[KgEntity]:
        return [
            KgEntity(
                qid=eid,
                kind="state_code",
                labels={"en": f"{lab} ({eid.split(':')[0].upper()} {eid.split(':')[1]})"},
                descriptions={"en": "state as coded by a Gleditsch-Ward / Correlates-of-War state code"},
            )
            for eid, lab in sorted(self.code_labels.items())
        ]


def merge_years(years: Iterable[int]) -> list[tuple[int, int]]:
    """Sorted runs of consecutive years: [1946, 1947, 1950] -> [(1946, 1947), (1950, 1950)]."""
    out: list[tuple[int, int]] = []
    for y in sorted(set(years)):
        if out and y == out[-1][1] + 1:
            out[-1] = (out[-1][0], y)
        else:
            out.append((y, y))
    return out


# --------------------------------------------------------------------------------------
# countrycode reference (CRAN), shared by the three connectors
# --------------------------------------------------------------------------------------

COUNTRYCODE_VERSION = "1.9.0"
COUNTRYCODE_URL = f"https://cran.r-project.org/src/contrib/countrycode_{COUNTRYCODE_VERSION}.tar.gz"
COUNTRYCODE_ARCHIVE_URL = (
    f"https://cran.r-project.org/src/contrib/Archive/countrycode/countrycode_{COUNTRYCODE_VERSION}.tar.gz"
)
COUNTRYCODE_MEMBER = "countrycode/data/codelist_panel.rda"
COUNTRYCODE_RIGHTS: dict[str, str] = {
    "licence": "GPL-3 (R package countrycode 1.9.0, Arel-Bundock, Enevoldsen & Yetman); code lists are facts",
    "licence_url": "https://cran.r-project.org/package=countrycode",
    "attribution": "Arel-Bundock, Enevoldsen & Yetman (2018), countrycode: An R package to convert country "
    "names and country codes. JOSS 3(28), 848",
}


def extract_tar_member(body: bytes, member: str, *, max_bytes: int = 20_000_000) -> bytes:
    """One regular-file member of a .tar.gz, bounded; links and path tricks are refused."""
    import io
    import tarfile

    from reva.kernel.errors import SourceResponseError

    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
            info = tf.getmember(member)
            if not info.isreg() or info.size > max_bytes:
                raise SourceResponseError(
                    f"tar member {member} is not a bounded regular file", retryable=False, kind="schema_drift"
                )
            f = tf.extractfile(info)
            if f is None:
                raise SourceResponseError(f"tar member {member} unreadable", retryable=False, kind="schema_drift")
            return f.read()
    except (KeyError, tarfile.TarError, EOFError, OSError) as e:
        raise SourceResponseError(
            f"tar member {member} missing or archive corrupt", retryable=False, kind="schema_drift"
        ) from e


def crosswalk_from_countrycode_tarball(body: bytes, known_keys: frozenset[str] | None = None) -> StateCodeCrosswalk:
    rda = extract_tar_member(body, COUNTRYCODE_MEMBER)
    cw = StateCodeCrosswalk.from_countrycode_rda(rda, known_keys=known_keys)
    # the provenance is the archived tarball, not the extracted member
    cw.source_sha256 = hashlib.sha256(body).hexdigest()
    return cw


def read_csv_member(
    body: bytes, member_suffix: str, *, max_total: int = 64 * 1024 * 1024, encoding: str = "utf-8-sig"
) -> list[dict[str, str]]:
    """Rows of the single CSV member ending with ``member_suffix`` of a zip (bounded; declared encoding,
    strict decoding)."""
    import csv
    import io

    from reva.adapters.http.archive import read_zip_members
    from reva.kernel.errors import SourceResponseError

    members = read_zip_members(body, (member_suffix.lower(),), max_total=max_total)
    if len(members) != 1:
        raise SourceResponseError(
            f"expected one {member_suffix} member, found {sorted(members)}", retryable=False, kind="schema_drift"
        )
    (data,) = members.values()
    try:
        textdata = data.decode(encoding)
    except UnicodeDecodeError as e:
        raise SourceResponseError(f"{member_suffix} is not {encoding}", retryable=False, kind="schema_drift") from e
    # fully blank rows (e.g. trailing ",,,," lines) carry nothing and are skipped
    return [r for r in csv.DictReader(io.StringIO(textdata)) if any((v or "").strip() for v in r.values())]


def atlas_frame(rows: list[dict[str, Any]]) -> Any:
    """polars DataFrame in the atlas schema from row dicts (missing nullable keys -> null)."""
    import polars as pl

    from reva.atlas.tables import ATLAS_COLUMNS, ATLAS_SCHEMA

    if not rows:
        return pl.DataFrame(schema=ATLAS_SCHEMA)
    cols = {c: [r.get(c) for r in rows] for c in ATLAS_COLUMNS}
    return pl.DataFrame(cols, schema=ATLAS_SCHEMA)


#: ``ConnectorSpec.template`` used by the V3 atlas connectors (see atlas_unesco_whc): validated with a
#: placeholder while the Literal lacks ``atlas_bulk``.
TEMPLATE = "atlas_bulk"
_PLACEHOLDER_TEMPLATE = "naturalearth_asset"


def make_spec(**fields: Any) -> Any:
    from typing import get_args

    from reva.ports.connectors import ConnectorSpec

    if TEMPLATE in get_args(ConnectorSpec.model_fields["template"].annotation):
        return ConnectorSpec(template=TEMPLATE, **fields)
    checked = ConnectorSpec(template=_PLACEHOLDER_TEMPLATE, **fields)
    return checked.model_copy(update={"template": TEMPLATE})


# --------------------------------------------------------------------------------------
# Run-level helpers (atlas source platform, docs/dev/v3_atlas_interface.md section 7)
# --------------------------------------------------------------------------------------

COUNTRY_QIDS_FILE = "registry/entities/country_qids.json"


def load_qid_for_iso3() -> dict[str, str]:
    """ISO3 -> Wikidata QID from WDHARVEST's ``registry/entities/country_qids.json`` (empty when absent:
    every state then stays a ``gw:``/``cow:`` code entity with ``geopol:corresponds_to_iso3`` statements)."""
    from reva.ontology.registry import REGISTRY_ROOT

    path = REGISTRY_ROOT / "entities" / "country_qids.json"
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    out = {}
    for code, e in (doc.get("countries") or {}).items():
        q = e.get("qid") if isinstance(e, Mapping) else e
        if isinstance(q, str) and q.startswith("Q"):
            out[code] = q
    return out


async def fetch_countrycode(run: Any) -> tuple[StateCodeCrosswalk, str]:
    """Fetch (through the run's metered client), archive and parse the countrycode tarball."""
    from reva.kernel.errors import SourceResponseError

    media = ("application/x-gzip", "application/gzip", "application/octet-stream", "application/x-tar")
    try:
        raw = await run.get(
            COUNTRYCODE_URL, key=f"countrycode:{COUNTRYCODE_VERSION}", media_types=media, max_bytes=5_000_000
        )
    except SourceResponseError:
        # CRAN moves superseded releases to Archive/: same bytes, pinned version
        raw = await run.get(
            COUNTRYCODE_ARCHIVE_URL, key=f"countrycode:{COUNTRYCODE_VERSION}", media_types=media, max_bytes=5_000_000
        )
    cw = crosswalk_from_countrycode_tarball(raw.body, crosswalk_keys())
    sha = run.archive(raw)
    return cw, sha


def code_correspondence(resolver: StateEntityResolver, *, raw_sha256: str) -> list[KgStatement]:
    """``gw:<code>``/``cow:<code>`` -> ``geopol:corresponds_to_iso3`` (string literal), one statement per run
    of years in which the crosswalk resolves the code to a crosswalk entity (none for historical states)."""
    out = []
    for eid, years in sorted(resolver.unresolved.items()):
        system, code = eid.split(":")
        runs: dict[str, list[int]] = {}
        for y in sorted(years):
            r = resolver.crosswalk.resolve(system, int(code), date(y, 7, 1))
            if r.iso3:
                runs.setdefault(r.iso3, []).append(y)
        for iso3, ys in runs.items():
            for a, b in merge_years(ys):
                out.append(
                    KgStatement(
                        key=f"{eid}:iso3:{iso3}:{a}",
                        subject=eid,
                        property="geopol:corresponds_to_iso3",
                        value={"type": "string", "value": iso3},
                        start=f"{a}-01-01",
                        start_precision=9,
                        end=f"{b}-12-31",
                        end_precision=9,
                        qualifiers={
                            "method": "countrycode codelist_panel + STATE_CODE_POLICY (atlas_geopol_common)",
                            "years_observed": [y for y in ys if a <= y <= b],
                        },
                        raw_sha256=raw_sha256,
                        selector={"file": COUNTRYCODE_MEMBER},
                    )
                )
    return out


#: Dataset predicates used by the geopolitics connectors: pid -> (value kind, reva predicate, en label,
#: fr label, inverse name, inverse en label).
GEOPOL_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "ucdp:primary_party": (
        "item",
        "ucdp_primary_party",
        "has primary party (UCDP)",
        "a pour partie principale (UCDP)",
        "ucdp_primary_party_in",
        "primary party in (UCDP)",
    ),
    "ucdp:secondary_party": (
        "item",
        "ucdp_secondary_party",
        "has secondary party with troops (UCDP)",
        "a pour partie secondaire (UCDP)",
        "ucdp_secondary_party_in",
        "secondary party in (UCDP)",
    ),
    "ucdp:location": (
        "item",
        "ucdp_location",
        "located in (UCDP conflict location)",
        "localisation (UCDP)",
        "ucdp_location_of",
        "location of conflict (UCDP)",
    ),
    "ucdp:nonstate_side": (
        "item",
        "ucdp_nonstate_side",
        "has non-state side (UCDP)",
        "a pour partie non étatique (UCDP)",
        "ucdp_nonstate_side_in",
        "non-state side in (UCDP)",
    ),
    "ucdp:incompatibility": (
        "string",
        "ucdp_incompatibility",
        "incompatibility (UCDP)",
        "incompatibilité (UCDP)",
        None,
        None,
    ),
    "ucdp:conflict_type": (
        "string",
        "ucdp_conflict_type",
        "conflict type (UCDP)",
        "type de conflit (UCDP)",
        None,
        None,
    ),
    "ucdp:territory_name": (
        "string",
        "ucdp_territory_name",
        "contested territory (UCDP)",
        "territoire contesté (UCDP)",
        None,
        None,
    ),
    "ucdp:start_date": ("time", "ucdp_start_date", "start date (UCDP)", "date de début (UCDP)", None, None),
    "ucdp:episode_end": ("time", "ucdp_episode_end", "episode end date (UCDP)", "fin d'épisode (UCDP)", None, None),
    "ucdp:intensity_level": (
        "quantity",
        "ucdp_intensity_level",
        "intensity level (UCDP)",
        "niveau d'intensité (UCDP)",
        None,
        None,
    ),
    "ucdp:battle_related_deaths": (
        "quantity",
        "ucdp_battle_related_deaths",
        "battle-related deaths (UCDP)",
        "morts liées aux combats (UCDP)",
        None,
        None,
    ),
    "unga:voted_yes": (
        "item",
        "unga_voted_yes",
        "voted yes on (UNGA roll call)",
        "a voté oui (AGNU)",
        "unga_yes_vote_by",
        "yes vote by",
    ),
    "unga:voted_no": (
        "item",
        "unga_voted_no",
        "voted no on (UNGA roll call)",
        "a voté non (AGNU)",
        "unga_no_vote_by",
        "no vote by",
    ),
    "unga:voted_abstain": (
        "item",
        "unga_voted_abstain",
        "abstained on (UNGA roll call)",
        "s'est abstenu (AGNU)",
        "unga_abstention_by",
        "abstention by",
    ),
    "unga:resolution_symbol": (
        "string",
        "unga_resolution_symbol",
        "resolution symbol (UNGA)",
        "cote de la résolution (AGNU)",
        None,
        None,
    ),
    "unga:issue_code": (
        "string",
        "unga_issue_code",
        "issue code (Voeten UNGA data)",
        "code thématique (Voeten)",
        None,
        None,
    ),
    "unga:vote_date": ("time", "unga_vote_date", "date of vote (UNGA)", "date du vote (AGNU)", None, None),
    "cshapes:polity_period": (
        "other",
        "cshapes_polity_period",
        "polity period with fixed borders (CShapes 2.0)",
        "période à frontières fixes (CShapes 2.0)",
        None,
        None,
    ),
    "cshapes:ruled_by": (
        "item",
        "cshapes_ruled_by",
        "dependency ruled by (CShapes 2.0)",
        "dépendance de (CShapes 2.0)",
        "cshapes_rules",
        "rules dependency (CShapes 2.0)",
    ),
    "cshapes:shares_border_with": (
        "item",
        "cshapes_shares_border_with",
        "shares a border with (CShapes 2.0 topology)",
        "partage une frontière avec (CShapes 2.0)",
        "cshapes_shares_border_with",
        "shares a border with (CShapes 2.0 topology)",
    ),
    "geopol:corresponds_to_iso3": (
        "string",
        "corresponds_to_iso3",
        "state code corresponds to crosswalk entity",
        "code d'État correspondant à l'entité",
        None,
        None,
    ),
}


def kg_properties() -> list[Any]:
    from reva.kg.model import KgProperty

    return [
        KgProperty(
            pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv, inverse_label_en=ile
        )
        for p, (k, rp, le, lf, inv, ile) in GEOPOL_PROPERTIES.items()
    ]


def _kg_time(t: str | None, precision: int | None) -> Any:
    from reva.kg.model import KgTime

    return None if t is None else KgTime(t, int(precision or 11))


def to_kg_model(
    entities: Iterable[KgEntity], statements: Iterable[KgStatement], *, source_key: str, source_revision: str
) -> tuple[list[Any], list[Any]]:
    """Neutral records -> ``reva.kg.model`` records. The statement key becomes the source statement id; the
    raw selector is kept as the statement's own reference (``{"raw_sha256", "selector"}``)."""
    from reva.kg.model import KgEntity as MEntity
    from reva.kg.model import KgStatement as MStatement

    ents = [
        MEntity(
            qid=e.qid,
            kind=e.kind,
            labels=dict(e.labels),
            descriptions=dict(e.descriptions),
            source_revision=source_revision,
        )
        for e in entities
    ]
    sts = []
    for s in statements:
        value = s.value
        if value is not None and value.get("type") == "time":
            value = {"type": "time", "time": value["time"], "precision": value["precision"]}
        sts.append(
            MStatement(
                subject_qid=s.subject,
                property_pid=s.property,
                object_qid=s.object_qid,
                value=value,
                start=_kg_time(s.start, s.start_precision),
                end=_kg_time(s.end, s.end_precision),
                point_in_time=_kg_time(s.point_in_time, s.pit_precision),
                qualifiers=dict(s.qualifiers),
                references=({"raw_sha256": s.raw_sha256, "selector": dict(s.selector)},),
                source_key=source_key,
                source_revision=source_revision,
                statement_id=s.key,
                raw_sha256=s.raw_sha256,
            )
        )
    return ents, sts


def kg_available(engine: Any) -> bool:
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text("SELECT to_regclass('kg.statement') IS NOT NULL")).scalar_one()


def load_kg(
    engine: Any,
    store: Any,
    entities: Sequence[KgEntity],
    statements: Sequence[KgStatement],
    *,
    source_key: str,
    source_revision: str,
    raw_sha256s: Sequence[str],
    retrieved_at: Any,
    origin: str,
) -> dict[str, Any]:
    """Register the dataset predicates and load in bounded batches (entities first, then statements)."""
    from collections import Counter as _C

    from reva.kg.model import MAX_ENTITIES_PER_BATCH, MAX_STATEMENTS_PER_BATCH, load_batch, register_properties

    register_properties(engine, kg_properties())
    ents, sts = to_kg_model(entities, statements, source_key=source_key, source_revision=source_revision)
    totals: _C[str] = _C()
    rejected: _C[str] = _C()
    batches = [(ents[i : i + MAX_ENTITIES_PER_BATCH], []) for i in range(0, len(ents), MAX_ENTITIES_PER_BATCH)]
    step = min(MAX_STATEMENTS_PER_BATCH, 100_000)
    batches += [([], sts[i : i + step]) for i in range(0, len(sts), step)]
    for be, bs in batches:
        used = sorted({s.raw_sha256 for s in bs if s.raw_sha256} or set(raw_sha256s))
        rep = load_batch(
            engine,
            store,
            entities=be,
            statements=bs,
            source_key=source_key,
            raw_sha256s=used,
            retrieved_at=retrieved_at,
            origin=origin,
        )
        for k in (
            "entities_inserted",
            "entities_unchanged",
            "statements_inserted",
            "statements_unchanged",
            "statements_new_revision",
            "statements_conflicting",
            "statements_stale",
        ):
            totals[k] += getattr(rep, k)
        rejected.update(rep.rejected_counts)
    return {**totals, "rejected": dict(rejected), "batches": len(batches)}
