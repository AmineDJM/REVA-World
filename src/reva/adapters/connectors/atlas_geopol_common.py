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
from collections.abc import Iterable, Mapping
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
_p(_BOTH, 365, date(1922, 12, 30), date(1991, 12, 20), None, "Soviet Union (USSR)",
   "GW/COW use 365 for Russia throughout ('Russia (Soviet Union)'); the USSR period is not the ISO entity RUS")
_p(("gw",), 260, date(1949, 9, 21), date(1990, 10, 2), None, "German Federal Republic (West Germany)",
   "GW 260 = FRG until unification, then unified Germany (DEU)")
_p(("cow",), 260, date(1955, 5, 5), date(1990, 10, 2), None, "German Federal Republic (West Germany)",
   "COW 260 = FRG 1955-1990; unified Germany is COW 255 from 1990-10-03")
_p(_BOTH, 265, _OPEN, _FUTURE, None, "German Democratic Republic (East Germany)", "no current ISO entity (ISO 3166-3 DDDE)")
_p(_BOTH, 255, _OPEN, date(1945, 5, 8), None, "Germany (Prussia / German Reich)", "pre-1945 German state (GW: 'Germany (Prussia)')")
_p(_BOTH, 345, _OPEN, date(2006, 6, 4), None, "Yugoslavia / Serbia and Montenegro",
   "Yugoslavia (SFRY, FRY) and Serbia and Montenegro until Montenegro's independence (2006-06-03/05)")
_p(("cow",), 345, date(2006, 6, 5), _FUTURE, "SRB", "Serbia",
   "COW continues code 345 for Serbia after 2006-06-05 (GW uses 340)")
_p(_BOTH, 315, _OPEN, _FUTURE, None, "Czechoslovakia", "no current ISO entity (ISO 3166-3 CSHH); Czechia is 316")
_p(_BOTH, 678, _OPEN, date(1990, 5, 21), None, "Yemen Arab Republic (North Yemen)", "before unification 1990-05-22")
_p(("gw",), 678, date(1990, 5, 22), _FUTURE, "YEM", "Yemen",
   "GW continues code 678 for unified Yemen (COW uses 679)")
_p(_BOTH, 680, _OPEN, _FUTURE, None, "People's Democratic Republic of Yemen (South Yemen)", "merged into Yemen 1990-05-22")
_p(_BOTH, 816, _OPEN, date(1976, 7, 1), None, "Democratic Republic of Vietnam (North Vietnam)",
   "before reunification 1976-07-02")
_p(_BOTH, 816, date(1976, 7, 2), _FUTURE, "VNM", "Vietnam", "GW/COW 816 = unified Vietnam from 1976-07-02")
_p(_BOTH, 817, _OPEN, _FUTURE, None, "Republic of Vietnam (South Vietnam)", "no current ISO entity")
_p(_BOTH, 511, _OPEN, _FUTURE, None, "Zanzibar", "independent 1963-12-19 to 1964-04-26; no current ISO entity")
_p(_BOTH, 347, date(2008, 2, 17), _FUTURE, "XKX", "Kosovo",
   "crosswalk entity XKX is the World Bank code for Kosovo (not ISO 3166)")

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
    54: ("Dominica", "DMA"), 55: ("Grenada", "GRD"), 56: ("Saint Lucia", "LCA"),
    57: ("Saint Vincent and the Grenadines", "VCT"), 58: ("Antigua & Barbuda", "ATG"),
    60: ("Saint Kitts and Nevis", "KNA"), 221: ("Monaco", "MCO"), 223: ("Liechtenstein", "LIE"),
    331: ("San Marino", "SMR"), 232: ("Andorra", "AND"), 403: ("Sao Tome and Principe", "STP"),
    591: ("Seychelles", "SYC"), 935: ("Vanuatu", "VUT"), 970: ("Kiribati", "KIR"), 971: ("Nauru", "NRU"),
    972: ("Tonga", "TON"), 973: ("Tuvalu", "TUV"), 983: ("Marshall Islands", "MHL"), 986: ("Palau", "PLW"),
    987: ("Federated States of Micronesia", "FSM"), 990: ("Samoa/Western Samoa", "WSM"),
}
for _code, (_name, _iso) in GW_MICROSTATES.items():
    _p(("gw",), _code, _OPEN, _FUTURE, _iso, _name, "Gleditsch-Ward microstates list")


@dataclass(frozen=True)
class CodeResolution:
    code_system: str
    code: int
    iso3: str | None
    label: str | None
    reason: str  # policy | panel | panel_nearest_year | unmapped


class StateCodeCrosswalk:
    """GW/COW code + date -> crosswalk entity key (see module docstring)."""

    def __init__(self, panel_rows: Iterable[Mapping[str, Any]], *, source_sha256: str,
                 known_keys: frozenset[str] | None = None) -> None:
        self.source_sha256 = source_sha256
        self.known_keys = known_keys
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
        # "ucdp" = GW codes with UCDP's own deviations (its policy entries win, then GW's)
        systems = ("ucdp", "gw") if system == "ucdp" else (system,)
        for sys_ in systems:
            for start, end, iso3, label, _note in STATE_CODE_POLICY.get((sys_, code), []):
                if start <= when <= end:
                    return CodeResolution(system, code, self._known(iso3), label, "policy")
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
    s = (str(v).strip() if v is not None else "")
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
            KgEntity(qid=eid, kind="state_code",
                     labels={"en": f"{lab} ({eid.split(':')[0].upper()} {eid.split(':')[1]})"},
                     descriptions={"en": "state as coded by a Gleditsch-Ward / Correlates-of-War state code"})
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
                raise SourceResponseError(f"tar member {member} is not a bounded regular file", retryable=False,
                                          kind="schema_drift")
            f = tf.extractfile(info)
            if f is None:
                raise SourceResponseError(f"tar member {member} unreadable", retryable=False, kind="schema_drift")
            return f.read()
    except (KeyError, tarfile.TarError, EOFError, OSError) as e:
        raise SourceResponseError(f"tar member {member} missing or archive corrupt", retryable=False,
                                  kind="schema_drift") from e


def crosswalk_from_countrycode_tarball(body: bytes, known_keys: frozenset[str] | None = None) -> StateCodeCrosswalk:
    rda = extract_tar_member(body, COUNTRYCODE_MEMBER)
    cw = StateCodeCrosswalk.from_countrycode_rda(rda, known_keys=known_keys)
    # the provenance is the archived tarball, not the extracted member
    cw.source_sha256 = hashlib.sha256(body).hexdigest()
    return cw


def read_csv_member(body: bytes, member_suffix: str, *, max_total: int = 64 * 1024 * 1024,
                    encoding: str = "utf-8-sig") -> list[dict[str, str]]:
    """Rows of the single CSV member ending with ``member_suffix`` of a zip (bounded; declared encoding,
    strict decoding)."""
    import csv
    import io

    from reva.adapters.http.archive import read_zip_members
    from reva.kernel.errors import SourceResponseError

    members = read_zip_members(body, (member_suffix.lower(),), max_total=max_total)
    if len(members) != 1:
        raise SourceResponseError(f"expected one {member_suffix} member, found {sorted(members)}", retryable=False,
                                  kind="schema_drift")
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
