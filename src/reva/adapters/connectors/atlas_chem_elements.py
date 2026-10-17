"""Chemical elements and nuclides (PubChem periodic table + element pages) -> atlas + KG (V4, agent V4CHEM).

Source
------
PubChem (NCBI/NLM), no account:

* ``/rest/pug/periodictable/JSON`` — one row per element (118): atomic number, symbol, name, atomic mass,
  electron configuration, electronegativity, atomic radius, ionization energy, electron affinity, oxidation
  states, standard state, melting/boiling point, density, group/block, year discovered;
* ``/rest/pug_view/data/element/<Z>/JSON`` — one page per element aggregating contributors, each with its own
  ``Reference`` (source name, URL, licence URL/note): IUPAC CIAAW (atomic weights, isotopic compositions, history),
  Atomic Mass Data Center (AMDC, IAEA: NUBASE nuclide masses, half-lives, discovery years, decay modes),
  NIST Physical Measurement Laboratory (isotopic compositions, atomic weights), Jefferson Lab (crustal and oceanic
  abundance, history), Los Alamos National Laboratory (history), IUPAC IPTEI (CC BY-NC-ND 4.0).

The IAEA LiveChart API (``www-nds.iaea.org/relnsd``) answers a Cloudflare browser challenge to non-browser clients
(HTTP 403, 2026-10-17): it is not used (owner decision, docs/dev/v4_v4chem.md); the same AMDC nuclide data are
read here from the PubChem element pages, attributed to AMDC with AMDC's licence URL.

Licences
--------
PubChem's own content is US Government work (NLM copyright page); contributed sections keep their contributor's
terms, recorded per value (``licence_url`` / ``licence_note`` from the page's Reference). IUPAC IPTEI values are
CC BY-NC-ND 4.0: flagged ``licence_noncommercial`` (this connector does not parse IPTEI sections, the flag guards
future parsers). AMDC (IAEA disclaimer), CIAAW (bylaws) and Jefferson Lab (citation notice) need an owner licence
review before redistribution (flag ``licence_review``).

What is published
-----------------
*KG* (source key ``pubchem.pug``): entities ``element:<Z>`` (kind ``chemical_element``) and ``nuclide:<Z>-<A>[m]``
(kind ``isotope``); statements with the ``pubchem:*`` predicates of ``registry/kg/predicates_chem.yaml``.
*Atlas* (``source = pubchem``): ``elements`` (numeric periodic-table properties, atomic weights per contributor,
crustal/oceanic abundance) and ``nuclides`` (atomic mass, half-life, discovery year, isotopic abundance).
Geography: intrinsic properties use ``provider_geo = not_geographic``; abundances ``earth_crust`` /
``earth_oceans`` (the provider's own scope); ``iso3`` is null for all. Period = the retrieval year (values as
published at retrieval, flag ``period_semantics``), never a measurement year.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from reva.adapters.connectors.atlas_chem_common import (
    NOT_GEOGRAPHIC,
    PERIODIC_TABLE_URL,
    PUBCHEM_HOST,
    Element,
    atlas_decimal,
    atlas_frame,
    canon,
    code_digest,
    err,
    kg_decimal,
    load_kg,
    parse_periodic_table,
    quantity,
    slug,
    string_value,
    text_value,
    to_decimal,
    trim_registration,
)
from reva.kernel.errors import SourceResponseError

CONNECTOR_NAME = "atlas.chem_elements"
CONNECTOR_VERSION = "1.0.1"
PARSER_VERSION = "atlas-chem-elements-1"
SOURCE_KEY = "pubchem.pug"
ATLAS_SOURCE = "pubchem"
DATASETS = ("elements", "nuclides")
MAX_PAGE_BYTES = 4_000_000
FIXTURE_ELEMENTS = (1, 6, 8, 26, 92)
KG_PREFIXES = ("pubchem:",)

RIGHTS: dict[str, Any] = {
    "licence": "PubChem (NLM): public domain US Government content; contributed sections under each contributor's terms",
    "licence_url": "https://www.nlm.nih.gov/web_policies.html#copyright",
    "attribution": "PubChem, National Center for Biotechnology Information (NCBI), https://pubchem.ncbi.nlm.nih.gov",
    "per_value_licences": "flags.licence_url / flags.licence_note per contributor (CIAAW, AMDC/IAEA, NIST PML, JLab, LANL)",
    "licence_review": ["AMDC/IAEA disclaimer", "CIAAW bylaws", "Jefferson Lab citation notice"],
}

#: Contributor SourceName -> short key (atlas indicator dimension and statement-key part). An unknown contributor
#: gets the slug of its SourceName (deterministic, never merged with a known one).
CONTRIBUTORS: dict[str, str] = {
    "PubChem": "pubchem",
    "PubChem Elements": "pubchem_elements",
    "Atomic Mass Data Center (AMDC), International Atomic Energy Agency (IAEA)": "amdc",
    "IUPAC Commission on Isotopic Abundances and Atomic Weights (CIAAW)": "ciaaw",
    "Jefferson Lab, U.S. Department of Energy": "jlab",
    "Los Alamos National Laboratory, U.S. Department of Energy": "lanl",
    "NIST Physical Measurement Laboratory": "nist_pml",
    "IUPAC Periodic Table of the Elements and Isotopes (IPTEI)": "iupac_iptei",
}
NONCOMMERCIAL = frozenset({"iupac_iptei"})
LICENCE_REVIEW = frozenset({"amdc", "ciaaw", "jlab"})

#: Periodic-table numeric columns -> (KG pid, unit). Units are those of PubChem's periodic table page headings.
PT_NUMERIC: dict[str, tuple[str, str | None]] = {
    "AtomicMass": ("pubchem:atomic_mass", "u"),
    "Electronegativity": ("pubchem:electronegativity", "Pauling scale"),
    "AtomicRadius": ("pubchem:atomic_radius", "pm"),
    "IonizationEnergy": ("pubchem:ionization_energy", "eV"),
    "ElectronAffinity": ("pubchem:electron_affinity", "eV"),
    "MeltingPoint": ("pubchem:melting_point", "K"),
    "BoilingPoint": ("pubchem:boiling_point", "K"),
    "Density": ("pubchem:density", "g/cm3"),
}
PT_TEXT: dict[str, str] = {
    "Symbol": "pubchem:element_symbol",
    "ElectronConfiguration": "pubchem:electron_configuration",
    "OxidationStates": "pubchem:oxidation_states",
    "StandardState": "pubchem:standard_state",
    "GroupBlock": "pubchem:group_block",
}
ABUNDANCE_UNITS = {"milligrams per kilogram": "mg/kg", "milligrams per liter": "mg/L"}
#: Half-life units of the AMDC/NUBASE tables -> (family, factor). Seconds family: SI prefixes of the second and
#: the exact minute/hour/day; year family: multiples of the year (NUBASE's year, not converted to seconds by REVA).
HALF_LIFE_UNITS: dict[str, tuple[str, Decimal]] = {
    "ys": ("s", Decimal("1e-24")),
    "zs": ("s", Decimal("1e-21")),
    "as": ("s", Decimal("1e-18")),
    "fs": ("s", Decimal("1e-15")),
    "ps": ("s", Decimal("1e-12")),
    "ns": ("s", Decimal("1e-9")),
    "us": ("s", Decimal("1e-6")),
    "ms": ("s", Decimal("1e-3")),
    "s": ("s", Decimal(1)),
    "m": ("s", Decimal(60)),
    "h": ("s", Decimal(3600)),
    "d": ("s", Decimal(86400)),
    "y": ("y", Decimal(1)),
    "ky": ("y", Decimal("1e3")),
    "My": ("y", Decimal("1e6")),
    "Gy": ("y", Decimal("1e9")),
    "Ty": ("y", Decimal("1e12")),
    "Py": ("y", Decimal("1e15")),
    "Ey": ("y", Decimal("1e18")),
    "Zy": ("y", Decimal("1e21")),
    "Yy": ("y", Decimal("1e24")),
}


# ============================================================================================== data classes
@dataclass(frozen=True)
class Ref:
    number: int
    source_name: str
    key: str
    url: str | None
    licence_url: str | None
    licence_note: str | None
    source_id: str | None
    anid: int | None

    def flags(self) -> dict[str, Any]:
        f: dict[str, Any] = {"provided_by": self.source_name}
        if self.licence_url:
            f["licence_url"] = self.licence_url
        if self.licence_note:
            f["licence_note"] = self.licence_note
        if self.key in NONCOMMERCIAL:
            f["licence_noncommercial"] = True
        if self.key in LICENCE_REVIEW:
            f["licence_review"] = True
        return f


@dataclass
class Cell:
    """One value read from an element page: verbatim text and where it is."""

    text: str
    ref: Ref
    section: str
    info_index: int
    item_index: int
    markup: Sequence[Mapping[str, Any]] = ()


@dataclass
class Nuclide:
    z: int
    a: int
    isomer: str  # "" for the ground state, "m", "m2", "n", ...
    symbol_text: str  # verbatim AMDC/CIAAW symbol, e.g. "47Fem"
    amdc: dict[str, Cell] = field(default_factory=dict)  # column -> cell
    compositions: list[tuple[Ref, Cell | None, Cell | None]] = field(default_factory=list)  # (ref, mass, abundance)

    @property
    def key(self) -> str:
        return f"nuclide:{self.z}-{self.a}{self.isomer}"


@dataclass
class ElementPage:
    z: int
    symbol: str
    raw_sha256: str
    refs: dict[int, Ref]
    atomic_weights: list[Cell] = field(default_factory=list)
    crustal: list[Cell] = field(default_factory=list)
    oceanic: list[Cell] = field(default_factory=list)
    history: list[Cell] = field(default_factory=list)
    nuclides: dict[str, Nuclide] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ============================================================================================== connector
def element_url(z: int) -> str:
    return f"https://{PUBCHEM_HOST}/rest/pug_view/data/element/{z}/JSON"


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="PubChem (NCBI/NLM) periodic table and element pages (contributors: IUPAC CIAAW, AMDC/IAEA, NIST PML, Jefferson Lab, LANL)",
        licence=RIGHTS["licence"],
        allowed_hosts=(PUBCHEM_HOST,),
        datasets=DATASETS,
        dataset_ids=("rest/pug/periodictable", "rest/pug_view/data/element"),
        cadence_seconds=90 * 86400,
        planned_requests=125,
        max_response_bytes=MAX_PAGE_BYTES,
        min_interval_seconds=0.4,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(sys.modules[__name__], sys.modules["reva.adapters.connectors.atlas_chem_common"]),
        field_mappings={"atlas": [f"{ATLAS_SOURCE}.{d}" for d in DATASETS], "kg": list(KG_PREFIXES)},
        notes="1 periodic-table request + 1 page per element (118); PubChem asks <= 5 requests/s. Option elements=[Z,...] for a partial run.",
    )


async def atlas_ingest(run: Any, *, load: bool = True) -> dict[str, Any]:
    """Fetch the periodic table and the element pages, archive every response, register the atlas tables and
    load the KG. A page that cannot be fetched or parsed is reported (``missing_pages``) and the run continues
    without it (its element keeps the periodic-table statements)."""
    opt = run.options.get("elements") if isinstance(run.options, Mapping) else None
    pt_raw = await run.get(PERIODIC_TABLE_URL, key="periodictable", max_bytes=1_000_000)
    pt_sha = run.archive(pt_raw)
    elements = parse_periodic_table(pt_raw.body)
    wanted: list[int]
    if opt:
        wanted = sorted({int(x) for x in opt})
    elif run.origin == "test_fixture":
        wanted = list(FIXTURE_ELEMENTS)
    else:
        wanted = [e.z for e in elements]
    by_z = {e.z: e for e in elements}
    pages: list[ElementPage] = []
    missing: list[dict[str, Any]] = []
    for z in wanted:
        if z not in by_z:
            missing.append({"z": z, "reason": "not in periodic table"})
            continue
        try:
            raw = await run.get(element_url(z), key=f"element:{z}", max_bytes=MAX_PAGE_BYTES)
        except SourceResponseError as e:
            if getattr(e, "kind", "") in ("budget", "budget_exhausted"):
                raise
            missing.append({"z": z, "reason": f"fetch: {str(e)[:200]}"})
            continue
        sha = run.archive(raw)
        try:
            pages.append(parse_element_page(raw.body, by_z[z], raw_sha256=sha))
        except SourceResponseError as e:
            missing.append({"z": z, "reason": f"parse: {str(e)[:200]}"})
    retrieved_at = pt_raw.retrieved_at
    complete = not opt and not missing and run.origin == "live"
    out: dict[str, Any] = {
        "elements": len(elements),
        "pages_parsed": len(pages),
        "missing_pages": missing,
        "nuclides": sum(len(p.nuclides) for p in pages),
        "page_warnings": [w for p in pages for w in p.warnings][:50],
    }
    raws = [pt_sha, *(p.raw_sha256 for p in pages)]
    if run.wants("elements"):
        rows = element_rows(elements, pages, pt_sha=pt_sha, retrieved_at=retrieved_at)
        reg = run.register(
            dataset="elements",
            frame=atlas_frame(rows),
            raw_sha256s=raws,
            coverage_note="Element properties as published by PubChem (periodic table + element pages), per contributor.",
            rights=RIGHTS,
        )
        out["atlas_elements"] = trim_registration(reg)
    if run.wants("nuclides"):
        nrows = nuclide_rows(pages, retrieved_at=retrieved_at)
        if nrows:
            reg = run.register(
                dataset="nuclides",
                frame=atlas_frame(nrows),
                raw_sha256s=[p.raw_sha256 for p in pages],
                coverage_note="Nuclide masses, half-lives, discovery years (AMDC) and isotopic abundances (CIAAW, NIST) via PubChem element pages.",
                rights=RIGHTS,
            )
            out["atlas_nuclides"] = trim_registration(reg)
    if load:
        ents, stmts = kg_build(elements, pages, pt_sha=pt_sha, retrieved_at=retrieved_at)
        out["kg"] = load_kg(
            run.engine,
            run.store,
            entities=ents,
            statements=stmts,
            source_key=SOURCE_KEY,
            raw_sha256s=raws,
            retrieved_at=retrieved_at,
            origin=run.origin,
            parameters={"connector": CONNECTOR_NAME, "parser": PARSER_VERSION, "elements": len(pages)},
            prefixes=KG_PREFIXES,
            retract_prefix="el:" if complete else None,
        )
    return out


# ============================================================================================== page parsing
def _find(section: Mapping[str, Any], heading: str) -> Mapping[str, Any] | None:
    for s in section.get("Section") or []:
        if s.get("TOCHeading") == heading:
            return s  # type: ignore[no-any-return]
        r = _find(s, heading)
        if r is not None:
            return r
    return None


def _strings(info: Mapping[str, Any]) -> list[tuple[str, Sequence[Mapping[str, Any]]]]:
    v = info.get("Value") or {}
    out = []
    for x in v.get("StringWithMarkup") or []:
        if isinstance(x, Mapping) and isinstance(x.get("String"), str):
            out.append((x["String"], x.get("Markup") or ()))
    return out


def parse_refs(record: Mapping[str, Any]) -> dict[int, Ref]:
    out: dict[int, Ref] = {}
    for r in record.get("Reference") or []:
        n = r.get("ReferenceNumber")
        name = r.get("SourceName")
        if not isinstance(n, int) or not isinstance(name, str):
            raise err("element page: malformed Reference")
        out[n] = Ref(
            number=n,
            source_name=name,
            key=CONTRIBUTORS.get(name, slug(name)),
            url=r.get("URL"),
            licence_url=r.get("LicenseURL"),
            licence_note=r.get("LicenseNote"),
            source_id=r.get("SourceID"),
            anid=r.get("ANID"),
        )
    return out


def parse_element_page(body: bytes, element: Element, *, raw_sha256: str) -> ElementPage:
    try:
        doc = json.loads(body)
        rec = doc["Record"]
    except (ValueError, KeyError, TypeError) as e:
        raise err(f"element {element.z}: unexpected JSON ({e})") from None
    if str(rec.get("RecordNumber")) != str(element.z) and str(rec.get("RecordType", "")).lower() == "element":
        raise err(f"element page {element.z}: RecordNumber {rec.get('RecordNumber')!r}")
    refs = parse_refs(rec)
    page = ElementPage(element.z, element.symbol, raw_sha256, refs)

    def cells(heading: str) -> list[Cell]:
        sec = _find(rec, heading)
        out: list[Cell] = []
        if sec is None:
            return out
        for i, info in enumerate(sec.get("Information") or []):
            ref = refs.get(info.get("ReferenceNumber"))
            if ref is None:
                page.warnings.append(f"Z={element.z} {heading}: information {i} without a known reference")
                continue
            for j, (s, mk) in enumerate(_strings(info)):
                out.append(Cell(s, ref, heading, i, j, mk))
        return out

    page.atomic_weights = cells("Atomic Weight")
    page.crustal = cells("Estimated Crustal Abundance")
    page.oceanic = cells("Estimated Oceanic Abundance")
    hist = _find(rec, "History")
    if hist is not None:
        for i, info in enumerate(hist.get("Information") or []):
            ref = refs.get(info.get("ReferenceNumber"))
            if ref is None:
                continue
            for j, (s, mk) in enumerate(_strings(info)):
                if s.strip():
                    page.history.append(Cell(s, ref, "History", i, j, mk))
    _parse_amdc(rec, page, refs)
    _parse_compositions(rec, page, refs)
    return page


_NUCLIDE_RE = re.compile(r"^\s*([0-9]{1,3})([A-Z][a-z]{0,2})([a-z][0-9]?)?\s*$")


def nuclide_id(text: str, z: int, symbol: str) -> tuple[int, str] | None:
    """(A, isomer suffix) of an AMDC/CIAAW nuclide symbol on the page of element ``symbol``; None if it names
    another element (never guessed)."""
    t = text.strip()
    if z == 1 and t in ("2D", "3T"):  # deuterium / tritium: IUPAC-accepted symbols for 2H and 3H (NIST tables)
        return int(t[0]), ""
    m = re.match(r"^([0-9]{1,3})" + re.escape(symbol) + r"([a-z][0-9]?)?$", t)
    if not m:
        return None
    return int(m.group(1)), m.group(2) or ""


def _columns(sec: Mapping[str, Any], refs: Mapping[int, Ref]) -> dict[int, dict[str, list[tuple[int, int, str, Any]]]]:
    """ReferenceNumber -> column Name -> [(info index, item index, text, markup)]."""
    out: dict[int, dict[str, list[tuple[int, int, str, Any]]]] = {}
    for i, info in enumerate(sec.get("Information") or []):
        n = info.get("ReferenceNumber")
        name = info.get("Name")
        if n not in refs or not isinstance(name, str):
            continue
        out.setdefault(n, {}).setdefault(name, []).extend((i, j, s, mk) for j, (s, mk) in enumerate(_strings(info)))
    return out


AMDC_COLUMNS = (
    "Nuclide",
    "Atomic Mass and Uncertainty [u]",
    "Half Life and Uncertainty",
    "Discovery Year",
    "Decay Modes, Intensities and Uncertainties [%]",
)


def _parse_amdc(rec: Mapping[str, Any], page: ElementPage, refs: Mapping[int, Ref]) -> None:
    sec = _find(rec, "Atomic Mass, Half Life, and Decay")
    if sec is None:
        page.warnings.append(f"Z={page.z}: no AMDC nuclide table")
        return
    for n, cols in _columns(sec, refs).items():
        if set(cols) != set(AMDC_COLUMNS):
            raise err(f"Z={page.z}: AMDC columns changed: {sorted(cols)}")
        lens = {len(v) for v in cols.values()}
        if len(lens) != 1:
            raise err(f"Z={page.z}: AMDC columns of unequal length {sorted(lens)}")
        for k, (i, j, s, mk) in enumerate(cols["Nuclide"]):
            nid = nuclide_id(s, page.z, page.symbol)
            if nid is None:
                page.warnings.append(f"Z={page.z}: AMDC nuclide {s!r} not of this element")
                continue
            nu = page.nuclides.setdefault(f"{nid[0]}{nid[1]}", Nuclide(page.z, nid[0], nid[1], s.strip()))
            for col in AMDC_COLUMNS[1:]:
                ci, cj, cs, cmk = cols[col][k]
                nu.amdc[col] = Cell(cs, refs[n], sec["TOCHeading"], ci, cj, cmk or ())
            nu.amdc["Nuclide"] = Cell(s, refs[n], sec["TOCHeading"], i, j, mk or ())


def _parse_compositions(rec: Mapping[str, Any], page: ElementPage, refs: Mapping[int, Ref]) -> None:
    sec = _find(rec, "Isotope Mass and Abundance")
    if sec is None:
        return
    for n, cols in _columns(sec, refs).items():
        iso = cols.get("Isotope")
        mass_col = next((v for k, v in cols.items() if k.startswith("Atomic Mass")), None)
        ab_col = next((v for k, v in cols.items() if k.startswith("Abundance")), None)
        if not iso:
            continue
        for k, (_i, _j, s, _mk) in enumerate(iso):
            nid = nuclide_id(s, page.z, page.symbol)
            if nid is None:
                page.warnings.append(f"Z={page.z}: isotope {s!r} not of this element")
                continue
            nu = page.nuclides.setdefault(f"{nid[0]}{nid[1]}", Nuclide(page.z, nid[0], nid[1], s.strip()))
            mc = ab = None
            if mass_col and k < len(mass_col):
                mi, mj, ms, mmk = mass_col[k]
                mc = Cell(ms, refs[n], sec["TOCHeading"], mi, mj, mmk or ())
            if ab_col and k < len(ab_col):
                ai, aj, as_, amk = ab_col[k]
                ab = Cell(as_, refs[n], sec["TOCHeading"], ai, aj, amk or ())
            nu.compositions.append((refs[n], mc, ab))


# ============================================================================================== value parsing
_PAREN_UNC_RE = re.compile(r"^([0-9.]+)\(([0-9]+)\)$")
_PM_RE = re.compile(r"^([0-9.]+)\s*±\s*([0-9.]+)(\s*\[Estimated\])?$")


def parse_uncertain(text: str) -> tuple[Decimal, str | None, bool] | None:
    """``55.845(2)`` / ``53.939 608(3)`` / ``45.015467 ± 0.000304 [Estimated]`` / ``55.845`` ->
    (value, uncertainty text verbatim, estimated). Intervals (``[1.00784,1.00811]``) -> None."""
    t = text.replace(" ", "").replace(" ", "").strip()
    m = _PM_RE.match(t)
    if m:
        v = to_decimal(m.group(1))
        return (v, m.group(2), bool(m.group(3))) if v is not None else None
    t2 = t.replace(" ", "")
    m = _PAREN_UNC_RE.match(t2)
    if m:
        v = to_decimal(m.group(1))
        return (v, f"({m.group(2)})", False) if v is not None else None
    v = to_decimal(t2)
    return (v, None, False) if v is not None else None


def parse_interval(text: str) -> tuple[Decimal, Decimal] | None:
    m = re.match(r"^\[\s*([0-9.  ]+)\s*,\s*([0-9.  ]+)\s*\]$", text.strip())
    if not m:
        return None
    lo, hi = to_decimal(m.group(1)), to_decimal(m.group(2))
    return (lo, hi) if lo is not None and hi is not None else None


def parse_abundance(cell: Cell) -> tuple[Decimal, str] | None:
    """``5.63×10<sup>4</sup> milligrams per kilogram`` -> (56300, "mg/kg"); the exponent is the superscript run
    right after "×10" (PubChem markup), never inferred from digit positions alone."""
    s = cell.text.strip()
    unit = None
    for long, short in ABUNDANCE_UNITS.items():
        if s.endswith(" " + long):
            unit = short
            s = s[: -len(long) - 1]
            break
    if unit is None:
        return None
    if "×10" in s:
        k = s.index("×10")
        mant = to_decimal(s[:k])
        exp_start = k + 3
        sup = [m for m in cell.markup if m.get("Type") == "Superscript" and m.get("Start") == exp_start]
        if mant is None or not sup or exp_start + int(sup[0]["Length"]) != len(s):
            return None
        e = s[exp_start:]
        if not re.match(r"^-?[0-9]+$", e):
            return None
        return mant.scaleb(int(e)), unit
    v = to_decimal(s)
    return (v, unit) if v is not None else None


def parse_half_life(text: str) -> tuple[str, Decimal, str | None] | None:
    """``12.32 y ± 0.02`` -> (unit family "s"|"y", value in that family, uncertainty verbatim incl. unit scale);
    ``Stable`` / ``Not-specified`` / lower limits (``>``) -> None."""
    m = re.match(r"^([0-9.]+(?:[eE][+-]?[0-9]+)?)\s+([A-Za-z]{1,2})(?:\s*±\s*(\S+))?$", text.strip())
    if not m:
        return None
    v = to_decimal(m.group(1))
    u = HALF_LIFE_UNITS.get(m.group(2))
    if v is None or u is None:
        return None
    return u[0], v * u[1], m.group(3)


def decay_modes(text: str) -> list[str]:
    return [p.strip() for p in text.split(";") if p.strip()]


# ============================================================================================== atlas rows
def _period(retrieved_at: datetime) -> tuple[date, date]:
    y = retrieved_at.year
    return date(y, 1, 1), date(y + 1, 1, 1)


def _row(
    dataset: str,
    indicator: str,
    geo: str,
    value: str | None,
    value_text: str | None,
    unit: str,
    flags: Mapping[str, Any],
    raw_sha256: str,
    selector: Mapping[str, Any],
    retrieved_at: datetime,
) -> dict[str, Any]:
    p0, p1 = _period(retrieved_at)
    f = {
        "period_semantics": "value as published by PubChem at retrieval (not a measurement year)",
        "retrieved_at": retrieved_at.isoformat(),
        **flags,
    }
    return {
        "source": ATLAS_SOURCE,
        "dataset": dataset,
        "indicator": indicator,
        "concept_id": None,
        "iso3": None,
        "provider_geo": geo,
        "entity_qid": None,
        "period_start": p0,
        "period_end": p1,
        "value": value,
        "value_text": value_text,
        "unit": unit,
        "flags": canon(f),
        "raw_sha256": raw_sha256,
        "selector": canon(selector),
    }


def element_rows(
    elements: Sequence[Element], pages: Sequence[ElementPage], *, pt_sha: str, retrieved_at: datetime
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for e in elements:
        base = {"element": e.key, "symbol": e.symbol, "provided_by": "PubChem periodic table"}
        for col, (pid, unit) in PT_NUMERIC.items():
            v = to_decimal(e.cells[col])
            if v is None:
                continue  # empty cell: not reported (no row; never a zero)
            rows.append(
                _row(
                    "elements",
                    f"{pid.split(':')[1]}|Z={e.z}",
                    NOT_GEOGRAPHIC,
                    atlas_decimal(v),
                    None,
                    unit or "",
                    base,
                    pt_sha,
                    {"file": "periodictable.json", "row": e.row, "column": col},
                    retrieved_at,
                )
            )
        yd = e.cells["YearDiscovered"].strip()
        if yd:
            rows.append(
                _row(
                    "elements",
                    f"year_discovered|Z={e.z}",
                    NOT_GEOGRAPHIC,
                    yd if yd.isdigit() else None,
                    None if yd.isdigit() else yd,
                    "year",
                    base,
                    pt_sha,
                    {"file": "periodictable.json", "row": e.row, "column": "YearDiscovered"},
                    retrieved_at,
                )
            )
    for p in pages:
        for c in p.atomic_weights:
            pu = parse_uncertain(c.text)
            iv = parse_interval(c.text) if pu is None else None
            flags: dict[str, Any] = {"element": f"element:{p.z}", **c.ref.flags(), "verbatim": c.text}
            if pu and pu[1]:
                flags["uncertainty"] = pu[1]
            if iv:
                flags["interval"] = [atlas_decimal(iv[0]), atlas_decimal(iv[1])]
            rows.append(
                _row(
                    "elements",
                    f"atomic_weight|Z={p.z}|provider={c.ref.key}",
                    NOT_GEOGRAPHIC,
                    atlas_decimal(pu[0]) if pu else None,
                    None if pu else c.text,
                    "u",
                    flags,
                    p.raw_sha256,
                    _sel(c),
                    retrieved_at,
                )
            )
        for cells, name, geo in (
            (p.crustal, "crustal_abundance", "earth_crust"),
            (p.oceanic, "oceanic_abundance", "earth_oceans"),
        ):
            for c in cells:
                pa = parse_abundance(c)
                rows.append(
                    _row(
                        "elements",
                        f"{name}|Z={p.z}|provider={c.ref.key}",
                        geo,
                        atlas_decimal(pa[0]) if pa else None,
                        None if pa else c.text,
                        pa[1] if pa else "as published",
                        {"element": f"element:{p.z}", **c.ref.flags(), "verbatim": c.text},
                        p.raw_sha256,
                        _sel(c),
                        retrieved_at,
                    )
                )
    return rows


def _sel(c: Cell) -> dict[str, Any]:
    return {
        "file": "element.json",
        "section": c.section,
        "information": c.info_index,
        "item": c.item_index,
        "reference": c.ref.number,
    }


def nuclide_rows(pages: Sequence[ElementPage], *, retrieved_at: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in pages:
        for nu in p.nuclides.values():
            nid = f"{nu.z}-{nu.a}{nu.isomer}"
            mass = nu.amdc.get("Atomic Mass and Uncertainty [u]")
            if mass is not None:
                pu = parse_uncertain(mass.text)
                if pu is not None:
                    f = {"nuclide": nu.key, **mass.ref.flags(), "verbatim": mass.text}
                    if pu[1]:
                        f["uncertainty"] = pu[1]
                    if pu[2]:
                        f["estimated"] = True
                    rows.append(
                        _row(
                            "nuclides",
                            f"atomic_mass|nuclide={nid}|provider=amdc",
                            NOT_GEOGRAPHIC,
                            atlas_decimal(pu[0]),
                            None,
                            "u",
                            f,
                            p.raw_sha256,
                            _sel(mass),
                            retrieved_at,
                        )
                    )
            hl = nu.amdc.get("Half Life and Uncertainty")
            if hl is not None and hl.text.strip():
                ph = parse_half_life(hl.text)
                f = {"nuclide": nu.key, **hl.ref.flags(), "verbatim": hl.text}
                if ph is None:
                    rows.append(
                        _row(
                            "nuclides",
                            f"half_life|nuclide={nid}",
                            NOT_GEOGRAPHIC,
                            None,
                            hl.text.strip(),
                            "as published",
                            f,
                            p.raw_sha256,
                            _sel(hl),
                            retrieved_at,
                        )
                    )
                else:
                    fam, v, unc = ph
                    f.update(
                        computed_by="reva",
                        conversion=f"unit scale to {fam}"
                        + ("" if fam == "s" else " (multiples of NUBASE's year; not converted to seconds)"),
                    )
                    if unc:
                        f["uncertainty_verbatim"] = unc
                    rows.append(
                        _row(
                            "nuclides",
                            f"half_life|nuclide={nid}|unit={fam}",
                            NOT_GEOGRAPHIC,
                            atlas_decimal(v),
                            None,
                            fam,
                            f,
                            p.raw_sha256,
                            _sel(hl),
                            retrieved_at,
                        )
                    )
            dy = nu.amdc.get("Discovery Year")
            if dy is not None and re.match(r"^[0-9]{4}$", dy.text.strip()):
                rows.append(
                    _row(
                        "nuclides",
                        f"discovery_year|nuclide={nid}",
                        NOT_GEOGRAPHIC,
                        dy.text.strip(),
                        None,
                        "year",
                        {"nuclide": nu.key, **dy.ref.flags()},
                        p.raw_sha256,
                        _sel(dy),
                        retrieved_at,
                    )
                )
            for ref, _mc, ab in nu.compositions:
                if ab is None or not ab.text.strip():
                    continue
                pu = parse_uncertain(ab.text)
                f = {"nuclide": nu.key, **ref.flags(), "verbatim": ab.text}
                if pu and pu[1]:
                    f["uncertainty"] = pu[1]
                interval = parse_interval(ab.text) if pu is None else None
                if interval:
                    f["interval"] = [atlas_decimal(interval[0]), atlas_decimal(interval[1])]
                rows.append(
                    _row(
                        "nuclides",
                        f"isotopic_abundance|nuclide={nid}|provider={ref.key}",
                        NOT_GEOGRAPHIC,
                        atlas_decimal(pu[0]) if pu else None,
                        None if pu else ab.text.strip(),
                        "mole fraction",
                        f,
                        p.raw_sha256,
                        _sel(ab),
                        retrieved_at,
                    )
                )
    return rows


# ============================================================================================== KG
def _ref_entry(url: str, retrieved_at: datetime, sel: Mapping[str, Any], ref: Ref | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {"P854": url, "P813": retrieved_at.date().isoformat(), "selector": dict(sel)}
    if ref is not None:
        r.update({"provided_by": ref.source_name, "contributor_url": ref.url, "licence_url": ref.licence_url})
        if ref.licence_note:
            r["licence_note"] = ref.licence_note
        if ref.key in NONCOMMERCIAL:
            r["licence_noncommercial"] = True
        if ref.key in LICENCE_REVIEW:
            r["licence_review"] = True
    return r


def kg_build(
    elements: Sequence[Element], pages: Sequence[ElementPage], *, pt_sha: str, retrieved_at: datetime
) -> tuple[list[Any], list[Any]]:
    """Entities and statements (``reva.kg.model`` objects). Statement ids are ``el:<Z>:...`` / ``el:<Z>:nu:...``
    built from provider identifiers (atomic number, AMDC nuclide symbol, contributor, item position)."""
    from reva.kg.model import KgEntity, KgStatement, KgTime

    rev = "sha256:" + pt_sha[:16]
    ents: list[Any] = []
    stmts: list[Any] = []

    def st(
        sid: str,
        subj: str,
        pid: str,
        raw: str,
        refs: Sequence[Mapping[str, Any]],
        *,
        obj: str | None = None,
        value: Mapping[str, Any] | None = None,
        quals: Mapping[str, Any] | None = None,
        pit: KgTime | None = None,
    ) -> None:
        stmts.append(
            KgStatement(
                subject_qid=subj,
                property_pid=pid,
                object_qid=obj,
                value=value,
                point_in_time=pit,
                qualifiers=dict(quals or {}),
                references=tuple(refs),
                source_key=SOURCE_KEY,
                source_revision="sha256:" + raw[:16],
                statement_id=sid,
                raw_sha256=raw,
            )
        )

    for e in elements:
        ents.append(
            KgEntity(
                qid=e.key,
                kind="chemical_element",
                labels={"en": e.name},
                descriptions={
                    "en": f"chemical element, atomic number {e.z}, symbol {e.symbol} (PubChem periodic table)"
                },
                aliases={"en": [e.symbol]},
                source_revision=rev,
            )
        )
        sel = {"file": "periodictable.json", "row": e.row}
        ref = [_ref_entry(PERIODIC_TABLE_URL, retrieved_at, sel)]
        for col, pid in PT_TEXT.items():
            if e.cells[col].strip():
                st(
                    f"el:{e.z}:{col}",
                    e.key,
                    pid,
                    pt_sha,
                    ref,
                    value=string_value(e.cells[col].strip()),
                    quals={"column": col},
                )
        for col, (pid, unit) in PT_NUMERIC.items():
            v = to_decimal(e.cells[col])
            if v is not None:
                st(
                    f"el:{e.z}:{col}",
                    e.key,
                    pid,
                    pt_sha,
                    ref,
                    value=quantity(v, unit),
                    quals={"column": col, "verbatim": e.cells[col]},
                )
        yd = e.cells["YearDiscovered"].strip()
        if yd:
            st(
                f"el:{e.z}:YearDiscovered",
                e.key,
                "pubchem:year_discovered",
                pt_sha,
                ref,
                value=string_value(yd),
                pit=KgTime(f"{int(yd):04d}", 9) if yd.isdigit() else None,
                quals={"column": "YearDiscovered"},
            )
    for p in pages:
        el = f"element:{p.z}"
        url = element_url(p.z)
        for c in p.atomic_weights:
            rr = [_ref_entry(url, retrieved_at, _sel(c), c.ref)]
            sid_aw = f"el:{p.z}:aw:{c.ref.key}:{c.info_index}:{c.item_index}"
            q: dict[str, Any] = {"provided_by": c.ref.source_name, "verbatim": c.text}
            pu = parse_uncertain(c.text)
            if pu is not None:
                if pu[1]:
                    q["uncertainty"] = pu[1]
                st(sid_aw, el, "pubchem:atomic_weight", p.raw_sha256, rr, value=quantity(pu[0], "u"), quals=q)
            elif (iv := parse_interval(c.text)) is not None:
                # an interval is not a point value: no amount is invented; the source's bounds are kept verbatim
                q["interval"] = [kg_decimal(iv[0]), kg_decimal(iv[1])]
                st(
                    sid_aw,
                    el,
                    "pubchem:atomic_weight_interval",
                    p.raw_sha256,
                    rr,
                    value=string_value(c.text.strip()),
                    quals=q,
                )
        for cells, pid, tag in (
            (p.crustal, "pubchem:crustal_abundance", "crust"),
            (p.oceanic, "pubchem:oceanic_abundance", "ocean"),
        ):
            for c in cells:
                pa = parse_abundance(c)
                if pa is None:
                    continue
                st(
                    f"el:{p.z}:{tag}:{c.ref.key}:{c.info_index}:{c.item_index}",
                    el,
                    pid,
                    p.raw_sha256,
                    [_ref_entry(url, retrieved_at, _sel(c), c.ref)],
                    value=quantity(pa[0], pa[1]),
                    quals={"provided_by": c.ref.source_name, "verbatim": c.text},
                )
        for c in p.history:
            st(
                f"el:{p.z}:hist:{c.ref.key}:{c.info_index}:{c.item_index}",
                el,
                "pubchem:history_text",
                p.raw_sha256,
                [_ref_entry(url, retrieved_at, _sel(c), c.ref)],
                value=text_value(c.text.strip()),
                quals={"provided_by": c.ref.source_name},
            )
        for nu in p.nuclides.values():
            nk = nu.key
            sym = f"{nu.a}{p.symbol}{nu.isomer}"
            ents.append(
                KgEntity(
                    qid=nk,
                    kind="isotope",
                    labels={"en": f"{p.symbol}-{nu.a}{nu.isomer}"},
                    descriptions={
                        "en": f"nuclide {sym} (Z={nu.z}, A={nu.a}{', isomeric state ' + nu.isomer if nu.isomer else ''}), as listed on the PubChem element page"
                    },
                    aliases={"en": [sym]},
                    source_revision="sha256:" + p.raw_sha256[:16],
                )
            )
            sid = f"el:{p.z}:nu:{nu.a}{nu.isomer}"
            first = nu.amdc.get("Nuclide")
            r0 = [
                _ref_entry(
                    url, retrieved_at, _sel(first) if first else {"file": "element.json"}, first.ref if first else None
                )
            ]
            st(
                f"{sid}:isotope_of",
                nk,
                "pubchem:isotope_of",
                p.raw_sha256,
                r0,
                obj=el,
                quals={"symbol_verbatim": nu.symbol_text},
            )
            st(
                f"{sid}:A",
                nk,
                "pubchem:mass_number",
                p.raw_sha256,
                r0,
                value=quantity(Decimal(nu.a)),
                quals={"from": "nuclide symbol"},
            )
            mass = nu.amdc.get("Atomic Mass and Uncertainty [u]")
            if mass is not None and (pu := parse_uncertain(mass.text)) is not None:
                q = {"provided_by": mass.ref.source_name, "verbatim": mass.text}
                if pu[1]:
                    q["uncertainty"] = pu[1]
                if pu[2]:
                    q["estimated"] = True
                st(
                    f"{sid}:mass:amdc",
                    nk,
                    "pubchem:nuclide_mass",
                    p.raw_sha256,
                    [_ref_entry(url, retrieved_at, _sel(mass), mass.ref)],
                    value=quantity(pu[0], "u"),
                    quals=q,
                )
            hl = nu.amdc.get("Half Life and Uncertainty")
            if hl is not None and hl.text.strip():
                rr = [_ref_entry(url, retrieved_at, _sel(hl), hl.ref)]
                st(
                    f"{sid}:half_life",
                    nk,
                    "pubchem:half_life",
                    p.raw_sha256,
                    rr,
                    value=string_value(hl.text.strip()),
                    quals={"provided_by": hl.ref.source_name},
                )
                ph = parse_half_life(hl.text)
                if ph is not None:
                    fam, v, unc = ph
                    pid = "pubchem:half_life_seconds" if fam == "s" else "pubchem:half_life_years"
                    q = {
                        "provided_by": hl.ref.source_name,
                        "verbatim": hl.text,
                        "computed_by": "reva",
                        "conversion": f"unit scale to {fam}",
                    }
                    if unc:
                        q["uncertainty_verbatim"] = unc
                    st(f"{sid}:half_life_{fam}", nk, pid, p.raw_sha256, rr, value=quantity(v, fam), quals=q)
            dy = nu.amdc.get("Discovery Year")
            if dy is not None and re.match(r"^[0-9]{4}$", dy.text.strip()):
                y = dy.text.strip()
                st(
                    f"{sid}:discovered",
                    nk,
                    "pubchem:nuclide_discovery_year",
                    p.raw_sha256,
                    [_ref_entry(url, retrieved_at, _sel(dy), dy.ref)],
                    value={"type": "time", "time": f"{y}", "precision": 9},
                    pit=KgTime(y, 9),
                    quals={"provided_by": dy.ref.source_name},
                )
            dm = nu.amdc.get("Decay Modes, Intensities and Uncertainties [%]")
            if dm is not None:
                for k, mode in enumerate(decay_modes(dm.text)):
                    st(
                        f"{sid}:decay:{k}",
                        nk,
                        "pubchem:decay_mode",
                        p.raw_sha256,
                        [_ref_entry(url, retrieved_at, _sel(dm), dm.ref)],
                        value=string_value(mode),
                        quals={
                            "provided_by": dm.ref.source_name,
                            "position": k,
                            "mode": re.split(r"[=≈<>~ ]", mode, maxsplit=1)[0],
                        },
                    )
            for cref, mc, ab in nu.compositions:
                if mc is not None and (pu := parse_uncertain(mc.text)) is not None:
                    q = {"provided_by": cref.source_name, "verbatim": mc.text}
                    if pu[1]:
                        q["uncertainty"] = pu[1]
                    st(
                        f"{sid}:mass:{cref.key}",
                        nk,
                        "pubchem:nuclide_mass",
                        p.raw_sha256,
                        [_ref_entry(url, retrieved_at, _sel(mc), cref)],
                        value=quantity(pu[0], "u"),
                        quals=q,
                    )
                if ab is not None and parse_uncertain(ab.text) is None and (ia := parse_interval(ab.text)) is not None:
                    q = {
                        "provided_by": cref.source_name,
                        "verbatim": ab.text,
                        "interval": [kg_decimal(ia[0]), kg_decimal(ia[1])],
                    }
                    st(
                        f"{sid}:abundance:{cref.key}",
                        nk,
                        "pubchem:isotopic_abundance_interval",
                        p.raw_sha256,
                        [_ref_entry(url, retrieved_at, _sel(ab), cref)],
                        value=string_value(ab.text.strip()),
                        quals=q,
                    )
                if ab is not None and (pab := parse_uncertain(ab.text)) is not None:
                    q = {"provided_by": cref.source_name, "verbatim": ab.text}
                    if pab[1]:
                        q["uncertainty"] = pab[1]
                    st(
                        f"{sid}:abundance:{cref.key}",
                        nk,
                        "pubchem:isotopic_abundance",
                        p.raw_sha256,
                        [_ref_entry(url, retrieved_at, _sel(ab), cref)],
                        value=quantity(pab[0], "mole fraction"),
                        quals=q,
                    )
    return ents, stmts


__all__ = [
    "CONNECTOR_NAME",
    "atlas_ingest",
    "atlas_source",
    "element_rows",
    "kg_build",
    "nuclide_id",
    "nuclide_rows",
    "parse_abundance",
    "parse_element_page",
    "parse_half_life",
    "parse_uncertain",
]
