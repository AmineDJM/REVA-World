"""CIA World Factbook country profiles -> atlas tables ``factbook.*`` (V3, agent A-FACTBOOK).

**Why this source.** The big multilateral sources (World Bank, IMF, UN, ILO …) skip small states and
territories. The Factbook profiled 261 entities — every sovereign state plus Taiwan, Kosovo, Western
Sahara, Greenland, Bermuda, the Crown dependencies, the French, British, US, Dutch, Australian, New
Zealand and Norwegian dependencies, Antarctica, the oceans, the EU and the World — with one fixed
template. It is the gap filler of last resort, never an official national statistic.

**Status of the source (verified 2026-10-15).** The CIA ended the Factbook on 2026-02-04
("One of CIA's oldest and most recognizable intelligence publications, The World Factbook, has
sunset." — cia.gov story "Spotlighting The World Factbook as We Bid a Fond Farewell");
``www.cia.gov/the-world-factbook/`` now redirects to that story. The last official zip archive was
the 2020 edition. The best archivable bulk form of the final edition is the CC0 mirror
``github.com/factbook/factbook.json`` (one JSON per entity, auto-updated weekly from the CIA site
since August 2021): its last data update is commit ``783ed27`` ("auto-update week 4",
2026-01-22); later commits only touch the README and move ``aj``/``am``/``gg`` between region
folders. Profiles are fetched one file per entity from ``raw.githubusercontent.com`` at the pinned
commit :data:`COMMIT`. **Provenance is a mirror**: every row says so in ``flags.edition``.

Two atlas sources (one module, two specs, budget key ``factbook.json`` shared with A-LAW's
``atlas.factbook``):

``atlas.factbook_profiles`` (261 requests)
  * ``figures_2026_01`` — curated numeric fields (:data:`NUMERIC`: area, borders, coastline,
    elevation, land use, population, age structure, vital rates, urbanisation, life expectancy,
    literacy, GDP figures, inflation, unemployment, budget, debt, current account, exports/imports,
    reserves, exchange rates, military expenditure, refugees …) as strict decimal strings, plus
    split shares (languages, religions, ethnic groups, export/import partners) and land-border
    lengths per neighbour (:func:`parse_shares`, :func:`parse_borders`).
  * ``texts_2026_01`` — every text leaf of every profile verbatim (HTML tags removed, entities
    decoded, wording unchanged), except the two "Diplomatic representation" sections (addresses,
    phone numbers). Indicator = slug of the JSON path; selector keeps the exact path.
``atlas.factbook_disputes`` (259 requests)
  * ``disputes_2024_04`` — "Transnational Issues > Disputes - international", verbatim, from mirror
    commit :data:`DISPUTES_COMMIT` (auto-update 2024-04-18), the last edition before the CIA
    removed the field (2024-04-25). Every row ``flags.disputed_wording_verbatim = true``.

**Honesty rules applied.**

* Every numeric row: ``flags.estimate = true`` (US-government compilation/estimate, never an
  official national statistic), ``flags.est_marker`` = whether the text itself says "est.",
  ``flags.source_text`` = the cleaned text the number was read from, the field note if any.
* Period: the year the Factbook states — "(2024 est.)" -> 2024; "(2020-25 est.)" ->
  [2020-01-01, 2026-01-01). A leaf without a year inherits it from the only dated sibling of the
  same field (Factbook layout: "male"/"female" under a dated "total"; ``flags.year_from_sibling``).
  Otherwise the as-published day of the file (``flags.period_basis = "as_published"``), e.g. area.
* Texts keep their wording; disputed entities (:data:`DISPUTED`) carry ``flags.disputed_territory``
  and any text mentioning a dispute or claim carries ``flags.mentions_dispute``.
* Stale profiles: the mirror's last change date of each file (:data:`FILE_LAST_CHANGED`, from
  ``git log -1`` per file at :data:`COMMIT`) is ``flags.mirror_last_changed`` and the period of
  as-published rows; Western Sahara (dropped by the Factbook in December 2020) is flagged stale.
* Geography: ``provider_geo`` = Factbook GEC code (upper case), mapped with scheme ``factbook_gec``
  (``registry/atlas/geo_aliases/a_factbook.yaml``). Partial territories (West Bank, Gaza Strip ⊂
  PSE; Svalbard, Jan Mayen ⊂ SJM; US Pacific Island Wildlife Refuges, Wake Island, Navassa ⊂ UMI)
  and non-ISO entities (Taiwan — not a crosswalk entity —, Akrotiri, Dhekelia, Ashmore and Cartier,
  Coral Sea Islands, Clipperton, Paracel and Spratly Islands, EU, World, oceans) stay unmapped,
  kept and listed; partial ones carry ``flags.partial_of_iso3``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import polars as pl

from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

SOURCE_KEY = "factbook.json"
ATLAS_SOURCE = "factbook"
NAME_PROFILES = "atlas.factbook_profiles"
NAME_DISPUTES = "atlas.factbook_disputes"
VERSION = "1.0.0"
PARSER_VERSION = "atlas-factbook-profiles/1"
HOST = "raw.githubusercontent.com"
REPO = "factbook/factbook.json"
COMMIT = "144d6977b2b01ac1cbd220de754c0a005616760b"  # data identical to DATA_COMMIT
DATA_COMMIT = "783ed27f7a1116663c10e7b39d162d0c4500505d"  # auto-update week 4, 2026-01-22
DISPUTES_COMMIT = "b1e7065ada45922848799b55393ced060cefdf92"  # auto-update week 16, 2024-04-18
DS_FIGURES = "figures_2026_01"
DS_TEXTS = "texts_2026_01"
DS_DISPUTES = "disputes_2024_04"
GEO_SCHEME = "factbook_gec"
MAX_BYTES = 2_000_000
TEXT_LIMIT = 3900
RIGHTS = {
    "licence": "Public domain (US Government work); mirror CC0-1.0",
    "licence_url": "https://github.com/factbook/factbook.json/blob/master/LICENSE.md",
    "attribution": "The World Factbook, Central Intelligence Agency (final edition, via the factbook/factbook.json mirror)",
    "provider": "CIA World Factbook (discontinued 2026-02-04), factbook/factbook.json mirror",
    "source_key": SOURCE_KEY,
}
EDITION = (
    "CIA World Factbook final online edition as mirrored by factbook/factbook.json "
    "(mirror data commit 783ed27, auto-update 2026-01-22; the CIA ended the Factbook on 2026-02-04)"
)
EDITION_DISPUTES = (
    "CIA World Factbook online edition as mirrored by factbook/factbook.json at commit b1e7065 "
    "(auto-update 2024-04-18), the last edition with 'Disputes - international' (removed 2024-04-25)"
)
NATURE = "US government (CIA) compilation/estimate; not an official national statistic"

#: region folder -> GEC codes at :data:`COMMIT` (``git ls-tree``; ``meta/`` excluded): 261 entities.
PATHS: dict[str, str] = {
    "africa": "ag ao bc bn by cd cf cg cm cn ct cv dj eg ek er et ga gb gh gv iv ke li lt ly ma mi ml mo mp mr mz "
    "ng ni od pu rw se sf sg sh sl so su to tp ts tz ug uv wa wi wz za zi",
    "antarctica": "ay bv fs hm",
    "australia-oceania": "aq as at bp ck cq cr cw fj fm fp gq kr kt nc ne nf nh nr nz pc ps rm tl tn tv um wf wq ws",
    "central-america-n-caribbean": "aa ac av bb bf bh bq cj cs cu do dr es gj gt ha ho jm mh nn nu pm rn rq sc st "
    "tb td tk uc vc vi vq",
    "central-asia": "aj am gg kg kz rs ti tx uz",
    "east-n-southeast-asia": "bm bx cb ch hk id ja kn ks la mc mg my pf pg pp rp sn th tt tw vm",
    "europe": "al an au ax be bk bo bu cy da dx ee ei en ez fi fo fr gi gk gm gr hr hu ic im it je jn kv lg lh lo "
    "ls lu md mj mk mn mt nl no pl po ri ro si sm sp sv sw sz uk up vt",
    "middle-east": "ae ba gz ir is iz jo ku le mu qa sa sy tu we ym",
    "north-america": "bd ca gl ip mx sb us",
    "oceans": "oo xo xq zh zn",
    "south-america": "ar bl br ci co ec fk gy ns pa pe sx uy ve",
    "south-asia": "af bg bt ce in io mv np pk",
    "world": "xx",
}
#: region folder -> GEC codes whose file carries "Disputes - international" at DISPUTES_COMMIT (259).
DISPUTES_PATHS: dict[str, str] = {
    **{k: v for k, v in PATHS.items() if k not in ("central-asia", "europe", "middle-east")},
    "central-asia": "kg kz rs ti tx uz",
    "europe": "al an au be bk bo bu cy da ee ei en ez fi fo fr gi gk gm gr hr hu ic im it je jn kv lg lh lo ls lu "
    "md mj mk mn mt nl no pl po ri ro si sm sp sv sw sz uk up vt",
    "middle-east": "ae aj am ba gg gz ir is iz jo ku le mu qa sa sy tu we ym",
}
#: mirror date of the last change of each file at COMMIT (``git log -1 --format=%cs -- <file>``).
_LAST_CHANGED: dict[str, str] = {
    "2021-01-01": "wi",
    "2024-02-22": "ax",
    "2024-04-18": "dx",
    "2025-10-07": "ay bv io jn sv sx wq",
    "2025-12-11": "ck fs hm kt nf",
    "2026-01-21": "aa ac an at av ba bb bd bx by cf cj cq cr cv do ei ek fj fk fm fo ga gj gl gq gt gy ho hu ic "
    "ip is ja kr kv la le lo lu ly mc mg mh mi mj mo mv mx ne nh no nz od pl pu ri rm ro rp rw sa se sg sh si sp "
    "su tk tl tp tx uc up vc vi vt xo xq zh",
    "2026-05-17": "aj am gg",  # only moved between region folders (content of the 2026-01-22 update)
}
FILE_LAST_CHANGED: dict[str, date] = {
    g: date.fromisoformat(d) for d, gs in _LAST_CHANGED.items() for g in gs.split()
}
DEFAULT_LAST_CHANGED = date(2026, 1, 22)
#: the moved files carry 2026-01-22 data: their as-published day is the data commit's day.
_MOVED_ONLY = {"aj", "am", "gg"}
STALE: dict[str, str] = {
    "wi": "the Factbook dropped Western Sahara in December 2020; the mirror file last changed 2021-01-01",
}
DISPUTED: dict[str, str] = {
    "wi": "Western Sahara: status disputed (Morocco / Sahrawi Arab Democratic Republic)",
    "kv": "Kosovo: independence (2008) not recognised by all states, including Serbia",
    "tw": "Taiwan: status disputed (claimed by the People's Republic of China)",
    "we": "West Bank: territory whose status is subject to the Israeli-Palestinian conflict",
    "gz": "Gaza Strip: territory whose status is subject to the Israeli-Palestinian conflict",
    "pf": "Paracel Islands: sovereignty disputed (China, Taiwan, Vietnam)",
    "pg": "Spratly Islands: sovereignty disputed (China, Taiwan, Vietnam, Philippines, Malaysia, Brunei)",
    "fk": "Falkland Islands (Islas Malvinas): sovereignty claimed by Argentina",
}
PARTIAL_OF: dict[str, str] = {
    "we": "PSE",
    "gz": "PSE",
    "sv": "SJM",
    "jn": "SJM",
    "um": "UMI",
    "wq": "UMI",
    "bq": "UMI",
}
AGGREGATES = {
    "xx": "World",
    "ee": "European Union",
    "oo": "Southern Ocean",
    "xo": "Indian Ocean",
    "xq": "Arctic Ocean",
    "zh": "Atlantic Ocean",
    "zn": "Pacific Ocean",
}
SKIP_SECTIONS = ("Diplomatic representation in the US", "Diplomatic representation from the US")


class FactbookProfileError(SourceResponseError):
    pass


def _err(msg: str) -> FactbookProfileError:
    return FactbookProfileError(msg, retryable=False, kind="schema_drift")


def url_for(region: str, gec: str, commit: str = COMMIT) -> str:
    return f"https://{HOST}/{REPO}/{commit}/{region}/{gec}.json"


def entities(paths: Mapping[str, str] = PATHS, gecs: Iterable[str] | None = None) -> list[tuple[str, str]]:
    want = set(gecs) if gecs is not None else None
    return [(r, g) for r, codes in paths.items() for g in codes.split() if want is None or g in want]


def last_changed(gec: str) -> date:
    return FILE_LAST_CHANGED.get(gec, DEFAULT_LAST_CHANGED)


def as_published(gec: str) -> tuple[date, date]:
    d = DEFAULT_LAST_CHANGED if gec in _MOVED_ONLY else last_changed(gec)
    return d, date.fromordinal(d.toordinal() + 1)


def _digest() -> str:
    return "sha256:" + hashlib.sha256(inspect.getsource(sys.modules[__name__]).encode()).hexdigest()


def atlas_source() -> list[AtlasSourceSpec]:
    common = {
        "version": VERSION,
        "source_key": SOURCE_KEY,
        "atlas_source": ATLAS_SOURCE,
        "provider": "CIA World Factbook (final edition; factbook/factbook.json CC0 mirror)",
        "licence": RIGHTS["licence"],
        "allowed_hosts": (HOST,),
        "cadence_seconds": 365 * 86400,
        "max_response_bytes": MAX_BYTES,
        "min_interval_seconds": 0.5,
        "parser_version": PARSER_VERSION,
        "code_digest": _digest(),
    }
    return [
        AtlasSourceSpec(
            name=NAME_PROFILES,
            datasets=(DS_FIGURES, DS_TEXTS),
            dataset_ids=(f"{REPO}@{COMMIT}",),
            planned_requests=sum(len(v.split()) for v in PATHS.values()),
            field_mappings={"commit": COMMIT, "data_commit": DATA_COMMIT, "numeric": sorted(NUMERIC)},
            notes="One JSON per Factbook entity at the pinned mirror commit; source discontinued.",
            **common,
        ),
        AtlasSourceSpec(
            name=NAME_DISPUTES,
            datasets=(DS_DISPUTES,),
            dataset_ids=(f"{REPO}@{DISPUTES_COMMIT}",),
            planned_requests=sum(len(v.split()) for v in DISPUTES_PATHS.values()),
            field_mappings={"commit": DISPUTES_COMMIT, "field": ["Transnational Issues", "Disputes - international"]},
            notes="Last mirror edition carrying 'Disputes - international' (removed by the CIA on 2024-04-25).",
            **common,
        ),
    ]


# ------------------------------------------------------------------------------------------ text
_TAGS = re.compile(r"<[^>]{0,300}>")
_BR = re.compile(r"<\s*(br|/p|p|li)\b[^>]{0,50}>", re.I)
_ENTITY = re.compile(r"&(#\d{1,6}|#x[0-9a-fA-F]{1,6}|[a-zA-Z]{2,8});")


def clean_text(s: str) -> str:
    """Remove HTML tags and decode entities; whitespace collapsed; wording unchanged."""
    import html

    t = _BR.sub(" ", s)
    t = _TAGS.sub(" ", t)
    t = _ENTITY.sub(lambda m: html.unescape(m.group(0)), t)
    t = "".join(" " if (ch < " " or ch == "\x7f") else ch for ch in t)
    return " ".join(t.replace(" ", " ").split())


def slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return out or "x"


def bounded_indicator(ind: str, limit: int = 200) -> str:
    """Atlas indicators are at most 200 characters: longer ones keep a prefix and a short hash."""
    if len(ind) <= limit:
        return ind
    return ind[: limit - 10] + "~" + hashlib.sha256(ind.encode()).hexdigest()[:9]


_DISPUTE_RE = re.compile(r"\b(disput\w*|claim\w*|contested|occupied|annex\w*)\b", re.I)


# --------------------------------------------------------------------------------------- numbers
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_SCALE = {"thousand": 3, "million": 6, "billion": 9, "trillion": 12}
_YEAR = re.compile(r"\((?P<y1>(?:19|20)\d{2})(?:\s*[-–]\s*(?P<y2>(?:19|20)?\d{2}))?(?P<est>\s*est\.?)?\)")
_LABEL = re.compile(r"^[A-Za-z][A-Za-z ,'/()-]{0,60}:\s+")
_MONEY = re.compile(rf"^(?P<s1>-)?\$(?P<s2>-)?(?P<num>{_NUM})(?:\s+(?P<scale>thousand|million|billion|trillion)\b)?")
_PLAIN = re.compile(rf"^(?P<s1>-)?(?P<num>{_NUM})(?:\s*(?P<scale>thousand|million|billion|trillion)\b)?")


def decimal_str(d: Decimal) -> str:
    """Strict decimal text (no exponent, no trailing zeros, no '-0')."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def _scaled(num: str, scale: str | None, negative: bool) -> str | None:
    try:
        d = Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None
    if scale:
        d = d.scaleb(_SCALE[scale])
    if negative:
        d = -d
    return decimal_str(d)


def year_of(text: str) -> tuple[tuple[date, date] | None, bool]:
    """The first "(YYYY[-YY] [est.])" of ``text`` -> (half-open period, est marker)."""
    m = _YEAR.search(text)
    if not m:
        return None, False
    y1 = int(m.group("y1"))
    y2s = m.group("y2")
    y2 = y1
    if y2s:
        y2 = int(y2s) if len(y2s) == 4 else (y1 // 100) * 100 + int(y2s)
        if y2 < y1:
            y2 = y1
    return (date(y1, 1, 1), date(y2 + 1, 1, 1)), bool(m.group("est"))


class Spec:
    """One curated numeric Factbook field."""

    __slots__ = ("indicator", "path", "kind", "unit", "suffix", "year_keyed")

    def __init__(
        self,
        indicator: str,
        path: tuple[str, ...],
        kind: str,
        unit: str,
        suffix: str = "",
        year_keyed: bool = False,
    ) -> None:
        self.indicator, self.path, self.kind, self.unit = indicator, path, kind, unit
        self.suffix = re.compile(r"^\s*" + suffix) if suffix else None
        self.year_keyed = year_keyed


_G, _P, _E, _GV, _EN, _C, _M, _T, _ENV = (
    "Geography",
    "People and Society",
    "Economy",
    "Government",
    "Energy",
    "Communications",
    "Military and Security",
    "Transnational Issues",
    "Environment",
)
_PCT = r"%"
_SPECS: list[Spec] = [
    # geography
    Spec("area.total", (_G, "Area", "total"), "plain", "km2", r"sq km"),
    Spec("area.land", (_G, "Area", "land"), "plain", "km2", r"sq km"),
    Spec("area.water", (_G, "Area", "water"), "plain", "km2", r"sq km"),
    Spec("land_boundaries.total", (_G, "Land boundaries", "total"), "plain", "km", r"km\b"),
    Spec("coastline", (_G, "Coastline"), "plain", "km", r"km\b"),
    Spec("elevation.highest", (_G, "Elevation", "highest point"), "elev", "m"),
    Spec("elevation.lowest", (_G, "Elevation", "lowest point"), "elev", "m"),
    Spec("elevation.mean", (_G, "Elevation", "mean elevation"), "plain", "m", r"m\b"),
    Spec("land_use.agricultural", (_G, "Land use", "agricultural land"), "plain", "%", _PCT),
    Spec("land_use.arable", (_G, "Land use", "agricultural land: arable land"), "plain", "%", _PCT),
    Spec("land_use.permanent_crops", (_G, "Land use", "agricultural land: permanent crops"), "plain", "%", _PCT),
    Spec("land_use.permanent_pasture", (_G, "Land use", "agricultural land: permanent pasture"), "plain", "%", _PCT),
    Spec("land_use.forest", (_G, "Land use", "forest"), "plain", "%", _PCT),
    Spec("land_use.other", (_G, "Land use", "other"), "plain", "%", _PCT),
    Spec("irrigated_land", (_G, "Irrigated land"), "plain", "km2", r"sq km"),
    # people and society
    Spec("population.total", (_P, "Population", "total"), "plain", "persons", r"(?!%|\d)"),
    Spec("population.male", (_P, "Population", "male"), "plain", "persons", r"(?!%|\d)"),
    Spec("population.female", (_P, "Population", "female"), "plain", "persons", r"(?!%|\d)"),
    Spec("age_structure.0_14", (_P, "Age structure", "0-14 years"), "plain", "%", _PCT),
    Spec("age_structure.15_64", (_P, "Age structure", "15-64 years"), "plain", "%", _PCT),
    Spec("age_structure.65_plus", (_P, "Age structure", "65 years and over"), "plain", "%", _PCT),
    Spec("dependency_ratio.total", (_P, "Dependency ratios", "total dependency ratio"), "plain", "ratio", r"(?!%)"),
    Spec("dependency_ratio.youth", (_P, "Dependency ratios", "youth dependency ratio"), "plain", "ratio", r"(?!%)"),
    Spec("dependency_ratio.elderly", (_P, "Dependency ratios", "elderly dependency ratio"), "plain", "ratio", r"(?!%)"),
    Spec("median_age.total", (_P, "Median age", "total"), "plain", "years", r"years"),
    Spec("median_age.male", (_P, "Median age", "male"), "plain", "years", r"years"),
    Spec("median_age.female", (_P, "Median age", "female"), "plain", "years", r"years"),
    Spec("population_growth_rate", (_P, "Population growth rate"), "plain", "%", _PCT),
    Spec("birth_rate", (_P, "Birth rate"), "plain", "births per 1,000 population", r"births/1,000 population"),
    Spec("death_rate", (_P, "Death rate"), "plain", "deaths per 1,000 population", r"deaths/1,000 population"),
    Spec(
        "net_migration_rate",
        (_P, "Net migration rate"),
        "plain",
        "migrants per 1,000 population",
        r"migrant\(s\)/1,000 population",
    ),
    Spec("urban_population", (_P, "Urbanization", "urban population"), "plain", "% of total population", _PCT),
    Spec("urbanization_rate", (_P, "Urbanization", "rate of urbanization"), "plain", "% annual rate of change", _PCT),
    Spec("sex_ratio.total", (_P, "Sex ratio", "total population"), "plain", "males per female", r"male\(s\)/female"),
    Spec("sex_ratio.at_birth", (_P, "Sex ratio", "at birth"), "plain", "males per female", r"male\(s\)/female"),
    Spec(
        "maternal_mortality_ratio",
        (_P, "Maternal mortality ratio"),
        "plain",
        "deaths per 100,000 live births",
        r"deaths/100,000 live births",
    ),
    Spec(
        "infant_mortality.total",
        (_P, "Infant mortality rate", "total"),
        "plain",
        "deaths per 1,000 live births",
        r"deaths/1,000 live births",
    ),
    Spec("life_expectancy.total", (_P, "Life expectancy at birth", "total population"), "plain", "years", r"years"),
    Spec("life_expectancy.male", (_P, "Life expectancy at birth", "male"), "plain", "years", r"years"),
    Spec("life_expectancy.female", (_P, "Life expectancy at birth", "female"), "plain", "years", r"years"),
    Spec("total_fertility_rate", (_P, "Total fertility rate"), "plain", "children born per woman", r"children"),
    Spec("literacy.total", (_P, "Literacy", "total population"), "plain", "%", _PCT),
    Spec("literacy.male", (_P, "Literacy", "male"), "plain", "%", _PCT),
    Spec("literacy.female", (_P, "Literacy", "female"), "plain", "%", _PCT),
    Spec("physician_density", (_P, "Physician density"), "plain", "physicians per 1,000 population", r"physicians"),
    Spec("hospital_bed_density", (_P, "Hospital bed density"), "plain", "beds per 1,000 population", r"beds"),
    Spec("obesity_adult", (_P, "Obesity - adult prevalence rate"), "plain", "%", _PCT),
    Spec(
        "health_expenditure_gdp",
        (_P, "Health expenditure", "Health expenditure (as % of GDP)"),
        "plain",
        "% of GDP",
        _PCT,
    ),
    Spec(
        "education_expenditure_gdp",
        (_P, "Education expenditure", "Education expenditure (% GDP)"),
        "plain",
        "% of GDP",
        _PCT,
    ),
    Spec(
        "school_life_expectancy.total",
        (_P, "School life expectancy (primary to tertiary education)", "total"),
        "plain",
        "years",
        r"years",
    ),
    Spec("drinking_water_improved.total", (_P, "Drinking water source", "improved: total"), "plain", "% of population", _PCT),
    Spec(
        "sanitation_improved.total",
        (_P, "Sanitation facility access", "improved: total"),
        "plain",
        "% of population",
        _PCT,
    ),
    Spec("tobacco_use.total", (_P, "Tobacco use", "total"), "plain", "%", _PCT),
    # economy
    Spec("gdp.real_ppp", (_E, "Real GDP (purchasing power parity)"), "money", "US$", year_keyed=True),
    Spec("gdp.real_growth", (_E, "Real GDP growth rate"), "plain", "%", _PCT, year_keyed=True),
    Spec("gdp.real_per_capita_ppp", (_E, "Real GDP per capita"), "money", "US$", year_keyed=True),
    Spec("gdp.official_exchange_rate", (_E, "GDP (official exchange rate)"), "money", "US$"),
    Spec("inflation", (_E, "Inflation rate (consumer prices)"), "plain", "%", _PCT, year_keyed=True),
    Spec("gdp_sector.agriculture", (_E, "GDP - composition, by sector of origin", "agriculture"), "plain", "% of GDP", _PCT),
    Spec("gdp_sector.industry", (_E, "GDP - composition, by sector of origin", "industry"), "plain", "% of GDP", _PCT),
    Spec("gdp_sector.services", (_E, "GDP - composition, by sector of origin", "services"), "plain", "% of GDP", _PCT),
    Spec(
        "gdp_end_use.household_consumption",
        (_E, "GDP - composition, by end use", "household consumption"),
        "plain",
        "% of GDP",
        _PCT,
    ),
    Spec(
        "gdp_end_use.government_consumption",
        (_E, "GDP - composition, by end use", "government consumption"),
        "plain",
        "% of GDP",
        _PCT,
    ),
    Spec(
        "gdp_end_use.fixed_investment",
        (_E, "GDP - composition, by end use", "investment in fixed capital"),
        "plain",
        "% of GDP",
        _PCT,
    ),
    Spec("gdp_end_use.exports", (_E, "GDP - composition, by end use", "exports of goods and services"), "plain", "% of GDP", _PCT),
    Spec("gdp_end_use.imports", (_E, "GDP - composition, by end use", "imports of goods and services"), "plain", "% of GDP", _PCT),
    Spec("industrial_production_growth", (_E, "Industrial production growth rate"), "plain", "%", _PCT),
    Spec("labor_force", (_E, "Labor force"), "plain", "persons", r"(?!%|\d)"),
    Spec("unemployment_rate", (_E, "Unemployment rate"), "plain", "%", _PCT, year_keyed=True),
    Spec("youth_unemployment.total", (_E, "Youth unemployment rate (ages 15-24)", "total"), "plain", "%", _PCT),
    Spec("poverty_line_population", (_E, "Population below poverty line"), "plain", "%", _PCT),
    Spec(
        "gini",
        (_E, "Gini Index coefficient - distribution of family income"),
        "plain",
        "Gini index (0-100)",
        r"(?!%)",
        year_keyed=True,
    ),
    Spec("income_share.lowest_10", (_E, "Household income or consumption by percentage share", "lowest 10%"), "plain", "%", _PCT),
    Spec("income_share.highest_10", (_E, "Household income or consumption by percentage share", "highest 10%"), "plain", "%", _PCT),
    Spec("remittances_gdp", (_E, "Remittances"), "plain", "% of GDP", _PCT, year_keyed=True),
    Spec("budget.revenues", (_E, "Budget", "revenues"), "money", "US$"),
    Spec("budget.expenditures", (_E, "Budget", "expenditures"), "money", "US$"),
    Spec("public_debt_gdp", (_E, "Public debt"), "plain", "% of GDP", _PCT, year_keyed=True),
    Spec("taxes_revenues_gdp", (_E, "Taxes and other revenues"), "plain", "% of GDP", _PCT),
    Spec("current_account_balance", (_E, "Current account balance"), "money", "US$", year_keyed=True),
    Spec("exports", (_E, "Exports"), "money", "US$", year_keyed=True),
    Spec("imports", (_E, "Imports"), "money", "US$", year_keyed=True),
    Spec("reserves", (_E, "Reserves of foreign exchange and gold"), "money", "US$", year_keyed=True),
    Spec("external_debt", (_E, "Debt - external"), "money", "US$", year_keyed=True),
    Spec("exchange_rate", (_E, "Exchange rates"), "plain", "national currency per US dollar", r"(?!%)", year_keyed=True),
    # energy, communications, environment
    Spec(
        "electricity_access",
        (_EN, "Electricity access", "electrification - total population"),
        "plain",
        "% of population",
        _PCT,
    ),
    Spec("internet_users", (_C, "Internet users", "percent of population"), "plain", "% of population", _PCT),
    Spec(
        "mobile_subscriptions_per_100",
        (_C, "Telephones - mobile cellular", "subscriptions per 100 inhabitants"),
        "plain",
        "per 100 inhabitants",
        r"(?!%)",
    ),
    Spec(
        "co2_emissions",
        (_ENV, "Carbon dioxide emissions", "total emissions"),
        "plain",
        "metric tonnes of CO2",
        r"metric tonnes of CO2",
    ),
    # military, transnational
    Spec("military_expenditure_gdp", (_M, "Military expenditures"), "plain", "% of GDP", _PCT, year_keyed=True),
    Spec("refugees", (_T, "Refugees and internally displaced persons", "refugees"), "plain", "persons", r"(?!%|\d)"),
    Spec("idps", (_T, "Refugees and internally displaced persons", "IDPs"), "plain", "persons", r"(?!%|\d)"),
    Spec(
        "stateless_persons",
        (_T, "Refugees and internally displaced persons", "stateless persons"),
        "plain",
        "persons",
        r"(?!%|\d)",
    ),
]
NUMERIC: dict[str, Spec] = {s.indicator: s for s in _SPECS}
#: list-like texts split into (name, share %) rows: indicator prefix -> path candidates
SHARES: dict[str, tuple[tuple[str, ...], ...]] = {
    "language_share": ((_P, "Languages", "Languages"), (_P, "Languages")),
    "religion_share": ((_P, "Religions"),),
    "ethnic_group_share": ((_P, "Ethnic groups"),),
    "export_partner_share": ((_E, "Exports - partners"),),
    "import_partner_share": ((_E, "Imports - partners"),),
}
PARTNER_SHARES = {"export_partner_share", "import_partner_share"}
#: Partner names used in Factbook lists that differ from the profiles' conventional short forms.
PARTNER_ALIASES: dict[str, str] = {
    "usa": "us",
    "us": "us",
    "uk": "uk",
    "uae": "ae",
    "south korea": "ks",
    "north korea": "kn",
    "czechia": "ez",
    "turkey": "tu",
    "turkiye": "tu",
    "democratic republic of the congo": "cg",
    "drc": "cg",
    "republic of the congo": "cf",
    "congo, democratic republic of the": "cg",
    "congo, republic of the": "cf",
    "burma": "bm",
    "macau": "mc",
    "hong kong": "hk",
    "taiwan": "tw",
    "russia": "rs",
    "vietnam": "vm",
    "laos": "la",
    "bosnia and herzegovina": "bk",
    "north macedonia": "mk",
    "eswatini": "wz",
    "cote d'ivoire": "iv",
    "côte d'ivoire": "iv",
    "timor-leste": "tt",
    "the bahamas": "bf",
    "bahamas": "bf",
    "the gambia": "ga",
    "gambia": "ga",
    "west bank": "we",
    "gaza strip": "gz",
    "holy see (vatican city)": "vt",
    "congo, republic of the": "cf",
    "bahamas, the": "bf",
    "gambia, the": "ga",
    "micronesia, federated states of": "fm",
    "korea, south": "ks",
    "s. korea": "ks",
    "st. kitts & nevis": "sc",
    "st. lucia": "st",
    "macedonia": "mk",
    "korea, north": "kn",
    "antigua & barbuda": "ac",
    "saint kitts & nevis": "sc",
    "trinidad & tobago": "td",
    "bosnia & herzegovina": "bk",
    "sao tome & principe": "tp",
    "st. vincent & the grenadines": "vc",
    "saint vincent & the grenadines": "vc",
    "vatican city": "vt",
}


def _node(doc: Any, path: Sequence[str]) -> Any:
    node = doc
    for k in path:
        if not isinstance(node, dict):
            return None
        if k in node:
            node = node[k]
        elif k + " " in node:  # the mirror spells "Area > total " with a trailing space
            node = node[k + " "]
        else:
            return None
    return node


def _text(node: Any) -> str | None:
    if isinstance(node, dict):
        node = node.get("text")
    return node if isinstance(node, str) and node.strip() else None


def _note(node: Any) -> str | None:
    if isinstance(node, dict):
        n = node.get("note")
        if isinstance(n, dict):
            n = n.get("text")
        if isinstance(n, str) and n.strip():
            return clean_text(n)[:400]
    return None


def parse_number(text: str, spec: Spec) -> tuple[str | None, dict[str, Any]]:
    """The leading number of a Factbook value text as a strict decimal string, else ``None``."""
    t = _LABEL.sub("", text, count=1) if not text.startswith("$") else text
    extra: dict[str, Any] = {}
    if spec.kind == "money":
        m = _MONEY.match(t)
        if not m:
            return None, extra
        return _scaled(m.group("num"), m.group("scale"), bool(m.group("s1") or m.group("s2"))), extra
    if spec.kind == "elev":
        m = re.match(rf"^(?P<name>.*?)\s*(?P<s1>-)?(?P<num>{_NUM})\s*m\b", t) or re.match(
            rf"^(?P<name>.*?)\s+(?P<s1>-)?(?P<num>{_NUM})\s*$", t
        )
        if not m:
            return None, extra
        name = m.group("name").strip(" :,-")
        if name:
            extra["feature"] = name[:200]
        return _scaled(m.group("num"), None, bool(m.group("s1"))), extra
    m = _PLAIN.match(t)
    if not m:
        return None, extra
    rest = t[m.end() :]
    if spec.suffix is not None and not spec.suffix.match(rest):
        return None, extra
    return _scaled(m.group("num"), m.group("scale"), bool(m.group("s1"))), extra


# --------------------------------------------------------------------------------- list splitting
_INVERTED = re.compile(r", (?:The|Republic of the|Democratic Republic of the|Federated States of|South|North)\b")


def split_top(text: str) -> list[str]:
    """Split on commas/semicolons outside parentheses/brackets (not thousands separators)."""
    out, depth, cur = [], 0, []
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        thousands = ch == "," and 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()
        inverted = ch == "," and _INVERTED.match(text, i) is not None  # "Congo, Republic of the 5%"
        if ch in ",;" and depth == 0 and not thousands and not inverted:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur).strip())
    return [s for s in out if s]


_TRAIL_YEAR = re.compile(r"\s*\((?:19|20)\d{2}(?:\s*[-–]\s*(?:19|20)?\d{2})?(?:\s*est\.?)?\)\s*$")
_SHARE = re.compile(r"^(?P<name>[^%]+?)\s+(?P<num>\d{1,3}(?:\.\d+)?)%(?P<rest>(?:\s*\([^()]*\))*)\s*$")
_BORDER = re.compile(rf"^(?P<name>[^\d]+?)\s+(?P<num>{_NUM})\s*km(?P<rest>(?:\s*\([^()]*\))*)\s*$")


def parse_shares(text: str) -> tuple[list[tuple[int, str, str, str]], int, tuple[date, date] | None, bool]:
    """``"Roman Catholic 47%, Muslim 4% (2021 est.)"`` -> ([(segment, name, value, qualifier)], skipped,
    period, est). Segments that are not "<name> <n>%" (e.g. "less than 1%", no share) are skipped."""
    period, est = None, False
    m = _TRAIL_YEAR.search(text)
    body = text
    if m:
        period, est = year_of(m.group(0))
        body = text[: m.start()]
    out, skipped, seen = [], 0, set()
    for i, seg in enumerate(split_top(body)):
        sm = _SHARE.match(seg)
        if not sm:
            skipped += 1
            continue
        name = sm.group("name").strip()
        low = name.lower()
        if re.search(r"\b(less than|more than|about|approximately|over|under)$", low) or name in seen:
            skipped += 1
            continue
        seen.add(name)
        out.append((i, name, decimal_str(Decimal(sm.group("num"))), sm.group("rest").strip()))
    return out, skipped, period, est


def parse_borders(text: str) -> tuple[list[tuple[int, str, str, str]], int]:
    out, skipped, seen = [], 0, set()
    for i, seg in enumerate(split_top(text)):
        bm = _BORDER.match(seg)
        if not bm or bm.group("name").strip() in seen:
            skipped += 1
            continue
        name = bm.group("name").strip()
        seen.add(name)
        out.append((i, name, decimal_str(Decimal(bm.group("num").replace(",", ""))), bm.group("rest").strip()))
    return out, skipped


def _norm_name(s: str) -> str:
    s = re.sub(r"\s*\((?:official|[^)]*)\)\s*$", "", s.strip()).lower()
    return " ".join(s.replace("’", "'").split())


def name_index(names: Mapping[str, Iterable[str]]) -> dict[str, str]:
    """normalised entity name -> GEC, from each profile's country names plus :data:`PARTNER_ALIASES`."""
    idx: dict[str, str] = {}
    ambiguous: set[str] = set()
    for gec, ns in names.items():
        for n in ns:
            k = _norm_name(n)
            if not k:
                continue
            if k in idx and idx[k] != gec:
                ambiguous.add(k)
            idx.setdefault(k, gec)
    for k in ambiguous:
        idx.pop(k, None)
    idx.update(PARTNER_ALIASES)
    return idx


def profile_names(doc: Mapping[str, Any]) -> list[str]:
    out = []
    for k in ("conventional short form", "conventional long form", "abbreviation", "local short form"):
        t = _text(_node(doc, ("Government", "Country name", k)))
        if t and t.strip().lower() != "none":
            out.append(clean_text(t))
    return out


# ------------------------------------------------------------------------------------------ rows
def _canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class Rows:
    """Column buffers for one atlas table."""

    def __init__(self, dataset: str) -> None:
        self.dataset = dataset
        self.cols: dict[str, list[Any]] = {c: [] for c in _COLUMNS}
        self.keys: set[tuple[str, str, date, date]] = set()
        self.duplicates = 0

    def add(
        self,
        *,
        indicator: str,
        geo: str,
        period: tuple[date, date],
        unit: str,
        flags: Mapping[str, Any],
        selector: Mapping[str, Any],
        raw_sha256: str,
        value: str | None = None,
        value_text: str | None = None,
        concept_id: str | None = None,
    ) -> bool:
        key = (indicator, geo, period[0], period[1])
        if key in self.keys:
            self.duplicates += 1
            return False
        self.keys.add(key)
        for c, v in (
            ("source", ATLAS_SOURCE),
            ("dataset", self.dataset),
            ("indicator", indicator),
            ("concept_id", concept_id),
            ("iso3", None),
            ("provider_geo", geo),
            ("entity_qid", None),
            ("period_start", period[0]),
            ("period_end", period[1]),
            ("value", value),
            ("value_text", value_text),
            ("unit", unit),
            ("flags", _canon(flags)),
            ("raw_sha256", raw_sha256),
            ("selector", _canon(selector)),
        ):
            self.cols[c].append(v)
        return True

    def __len__(self) -> int:
        return len(self.cols["indicator"])

    def frame(self) -> pl.DataFrame:
        from reva.atlas.tables import ATLAS_SCHEMA

        return pl.DataFrame(self.cols, schema=ATLAS_SCHEMA)


_COLUMNS = (
    "source dataset indicator concept_id iso3 provider_geo entity_qid period_start period_end value value_text unit "
    "flags raw_sha256 selector"
).split()


def entity_flags(gec: str) -> dict[str, Any]:
    f: dict[str, Any] = {"mirror_last_changed": last_changed(gec).isoformat()}
    if gec in DISPUTED:
        f["disputed_territory"] = DISPUTED[gec]
    if gec in PARTIAL_OF:
        f["partial_of_iso3"] = PARTIAL_OF[gec]
    if gec in STALE:
        f["stale_profile"] = STALE[gec]
    if gec in AGGREGATES:
        f["aggregate_or_non_country"] = True
    return f


def load_profile(body: bytes, region: str, gec: str) -> dict[str, Any]:
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise _err(f"{region}/{gec}.json is not JSON") from e
    if not isinstance(doc, dict) or not doc:
        raise _err(f"{region}/{gec}.json is not a non-empty object")
    return doc


def _leaves(node: Any, path: tuple[str, ...]) -> Iterable[tuple[tuple[str, ...], str]]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "text" and isinstance(v, str):
                yield path, v
            elif isinstance(v, str):  # e.g. "note": "<strong>note:</strong> ..."
                yield (*path, k), v
            else:
                yield from _leaves(v, (*path, k))


def parse_texts(doc: Mapping[str, Any], region: str, gec: str, sha: str, rows: Rows, stats: dict[str, int]) -> None:
    geo = gec.upper()
    file = f"{region}/{gec}.json"
    base = {"edition": EDITION, "provider_nature": NATURE, "transcription": "verbatim (HTML tags removed)"}
    base |= entity_flags(gec)
    period = as_published(gec)
    for path, raw in _leaves(doc, ()):
        if not path or path[0] in SKIP_SECTIONS or (len(path) > 1 and path[1] in SKIP_SECTIONS):
            continue
        text = clean_text(raw)
        if not text:
            continue
        flags = dict(base)
        if len(text) > TEXT_LIMIT:
            flags["value_text_truncated"] = True
            flags["full_length"] = len(text)
            text = text[:TEXT_LIMIT]
        if _DISPUTE_RE.search(text):
            flags["mentions_dispute"] = True
        indicator = bounded_indicator(".".join(slug(p) for p in path))
        if not rows.add(
            indicator=indicator,
            geo=geo,
            period=period,
            unit="text",
            flags=flags,
            selector={"file": file, "commit": COMMIT, "json_path": list(path)},
            raw_sha256=sha,
            value_text=text,
        ):
            stats["text_duplicate_paths"] = stats.get("text_duplicate_paths", 0) + 1
        else:
            stats["text_rows"] = stats.get("text_rows", 0) + 1


def _numeric_leaves(doc: Mapping[str, Any], spec: Spec) -> list[tuple[tuple[str, ...], str, str | None]]:
    """(json path, raw text, sibling-year source key) for a spec; year-keyed fields expand per year."""
    node = _node(doc, spec.path)
    if node is None:
        return []
    if spec.year_keyed and isinstance(node, dict):
        out = []
        field = spec.path[-1]
        pat = re.compile(r"^" + re.escape(field) + r" ((?:19|20)\d{2})$", re.I)
        for k, v in node.items():
            if pat.match(k) and _text(v):
                out.append(((*spec.path, k), _text(v) or "", None))
        if out:
            return out
    t = _text(node)
    return [(spec.path, t, None)] if t else []


def _sibling_year(doc: Mapping[str, Any], spec: Spec) -> tuple[tuple[date, date] | None, bool, str | None]:
    """The single dated sibling of a sub-field (e.g. "total" for "male")."""
    if len(spec.path) < 3:
        return None, False, None
    parent = _node(doc, spec.path[:-1])
    if not isinstance(parent, dict):
        return None, False, None
    found: dict[tuple[date, date], tuple[bool, str]] = {}
    for k, v in parent.items():
        t = _text(v)
        if t:
            p, est = year_of(t)
            if p:
                found.setdefault(p, (est, k))
    if len(found) == 1:
        (p, (est, k)), = found.items()
        return p, est, k
    return None, False, None


def parse_figures(
    doc: Mapping[str, Any],
    region: str,
    gec: str,
    sha: str,
    rows: Rows,
    stats: dict[str, int],
    names: Mapping[str, str],
) -> None:
    geo = gec.upper()
    file = f"{region}/{gec}.json"
    base = {"edition": EDITION, "provider_nature": NATURE, "estimate": True} | entity_flags(gec)
    pub = as_published(gec)
    for spec in _SPECS:
        node = _node(doc, spec.path)
        note = _note(node) or (_note(_node(doc, spec.path[:-1])) if len(spec.path) > 2 else None)
        for path, raw, _ in _numeric_leaves(doc, spec):
            text = clean_text(raw)
            value, extra = parse_number(text, spec)
            if value is None:
                stats[f"skipped:{spec.indicator}"] = stats.get(f"skipped:{spec.indicator}", 0) + 1
                continue
            flags = dict(base) | extra
            period, est = year_of(text)
            if period is None:
                period, est, sib = _sibling_year(doc, spec)
                if period is not None:
                    flags["year_from_sibling"] = sib
            if period is None:
                period = pub
                flags["period_basis"] = "as_published (no reference year stated)"
            flags["est_marker"] = est
            flags["source_text"] = text[:500]
            if note:
                flags["provider_note"] = note
            if spec.kind == "money":
                flags["currency_note"] = "US dollars as stated by the Factbook (see provider_note for base year)"
            if spec.indicator == "exchange_rate":
                cur = _text(_node(doc, (*spec.path, "Currency")))
                if cur:
                    flags["currency"] = clean_text(cur)[:200]
            if rows.add(
                indicator=spec.indicator,
                geo=geo,
                period=period,
                unit=spec.unit,
                flags=flags,
                selector={"file": file, "commit": COMMIT, "json_path": list(path)},
                raw_sha256=sha,
                value=value,
            ):
                stats["numeric_rows"] = stats.get("numeric_rows", 0) + 1
    # shares
    for prefix, candidates in SHARES.items():
        for path in candidates:
            raw = _text(_node(doc, path))
            if raw is None:
                continue
            text = clean_text(raw)
            items, skipped, period, est = parse_shares(text)
            stats[f"share_segments_skipped:{prefix}"] = stats.get(f"share_segments_skipped:{prefix}", 0) + skipped
            for seg, name, value, qual in items:
                flags = dict(base) | {
                    "est_marker": est,
                    "name": name,
                    "source_text": text[:1000],
                    "transcription": "list split into name/share by agent A-FACTBOOK, maintainer review pending",
                }
                if qual:
                    flags["qualifier"] = qual[:200]
                if period is None:
                    flags["period_basis"] = "as_published (no reference year stated)"
                if prefix in PARTNER_SHARES:
                    pg = names.get(_norm_name(name))
                    flags["partner_gec"] = pg.upper() if pg else None
                    flags["partner_iso3"] = _gec_iso3(pg) if pg else None
                if rows.add(
                    indicator=bounded_indicator(f"{prefix}|name={name}"),
                    geo=geo,
                    period=period or pub,
                    unit="%",
                    flags=flags,
                    selector={"file": file, "commit": COMMIT, "json_path": list(path), "segment": seg},
                    raw_sha256=sha,
                    value=value,
                ):
                    stats["share_rows"] = stats.get("share_rows", 0) + 1
            break
    raw = _text(_node(doc, (_G, "Land boundaries", "border countries")))
    if raw:
        text = clean_text(raw)
        items, skipped = parse_borders(text)
        stats["border_segments_skipped"] = stats.get("border_segments_skipped", 0) + skipped
        for seg, name, value, qual in items:
            pg = names.get(_norm_name(name))
            flags = dict(base) | {
                "name": name,
                "partner_gec": pg.upper() if pg else None,
                "partner_iso3": _gec_iso3(pg) if pg else None,
                "source_text": text[:1000],
                "period_basis": "as_published (no reference year stated)",
                "transcription": "list split into neighbour/length by agent A-FACTBOOK, maintainer review pending",
            }
            if qual:
                flags["qualifier"] = qual[:200]
            if rows.add(
                indicator=bounded_indicator(f"land_boundary|partner={name}"),
                geo=geo,
                period=pub,
                unit="km",
                flags=flags,
                selector={
                    "file": file,
                    "commit": COMMIT,
                    "json_path": [_G, "Land boundaries", "border countries"],
                    "segment": seg,
                },
                raw_sha256=sha,
                value=value,
            ):
                stats["border_rows"] = stats.get("border_rows", 0) + 1


def _gec_iso3(gec: str | None) -> str | None:
    if not gec:
        return None
    from reva.atlas.tables import country_iso3

    return country_iso3(gec.upper(), GEO_SCHEME)


def parse_disputes(body: bytes, region: str, gec: str, sha: str, rows: Rows, stats: dict[str, int]) -> None:
    doc = load_profile(body, region, gec)
    path = ("Transnational Issues", "Disputes - international")
    raw = _text(_node(doc, path))
    if raw is None:
        stats["disputes_absent"] = stats.get("disputes_absent", 0) + 1
        return
    text = clean_text(raw)
    flags = {
        "edition": EDITION_DISPUTES,
        "provider_nature": NATURE,
        "transcription": "verbatim (HTML tags removed)",
        "disputed_wording_verbatim": True,
    } | {k: v for k, v in entity_flags(gec).items() if k != "mirror_last_changed"}
    if len(text) > TEXT_LIMIT:
        flags["value_text_truncated"] = True
        flags["full_length"] = len(text)
        text = text[:TEXT_LIMIT]
    if rows.add(
        indicator="transnational_issues.disputes_international",
        geo=gec.upper(),
        period=(date(2024, 4, 18), date(2024, 4, 19)),
        unit="text",
        flags=flags,
        selector={"file": f"{region}/{gec}.json", "commit": DISPUTES_COMMIT, "json_path": list(path)},
        raw_sha256=sha,
        value_text=text,
    ):
        stats["dispute_rows"] = stats.get("dispute_rows", 0) + 1


# ---------------------------------------------------------------------------------------- ingest
async def _fetch_all(
    run: Any, paths: Mapping[str, str], commit: str, gecs: Iterable[str] | None
) -> tuple[list[tuple[str, str, bytes, str]], list[dict[str, Any]]]:
    got, failed = [], []
    for region, gec in entities(paths, gecs):
        try:
            raw = await run.get(
                url_for(region, gec, commit),
                key=f"{commit[:7]}/{region}/{gec}",
                media_types=("text/plain", "application/json"),
                max_bytes=MAX_BYTES,
            )
        except SourceResponseError as e:
            failed.append({"file": f"{region}/{gec}.json", "error": str(e)[:300], **dict(e.details or {})})
            continue
        sha = run.archive(raw)
        got.append((region, gec, raw.body, sha))
    return got, failed


def _coverage(registered: Mapping[str, Any]) -> dict[str, Any]:
    cov = registered.get("coverage") or {}
    return {
        "dataset_key": registered.get("dataset_key"),
        "row_count": registered.get("row_count"),
        "created": registered.get("created"),
        "countries": cov.get("countries"),
        "unmapped_provider_geo": [u.get("provider_geo") for u in registered.get("unmapped_provider_geo", [])],
    }


async def ingest_profiles(run: Any, gecs: Iterable[str] | None = None) -> dict[str, Any]:
    got, failed = await _fetch_all(run, PATHS, COMMIT, gecs)
    docs = [(r, g, load_profile(b, r, g), s) for r, g, b, s in got]
    names = name_index({g: profile_names(d) for _, g, d, _ in docs})
    stats: dict[str, int] = {}
    out: dict[str, Any] = {"files": len(got), "failed": failed, "commit": COMMIT}
    shas = [s for *_, s in docs]
    if run.wants(DS_FIGURES) and docs:
        rows = Rows(DS_FIGURES)
        for r, g, d, s in docs:
            parse_figures(d, r, g, s, rows, stats, names)
        reg = run.register(
            dataset=DS_FIGURES,
            frame=rows.frame(),
            raw_sha256s=shas,
            geo_scheme=GEO_SCHEME,
            rights=RIGHTS,
            coverage_note=f"CIA World Factbook final edition (mirror {DATA_COMMIT[:7]}, 2026-01-22): {len(NUMERIC)} "
            f"numeric fields + shares + land borders; {len(docs)} profiles; failed {len(failed)}. "
            "US-government estimates (flags.estimate), not official national statistics.",
        )
        out[DS_FIGURES] = _coverage(reg)
    if run.wants(DS_TEXTS) and docs:
        rows = Rows(DS_TEXTS)
        for r, g, d, s in docs:
            parse_texts(d, r, g, s, rows, stats)
        reg = run.register(
            dataset=DS_TEXTS,
            frame=rows.frame(),
            raw_sha256s=shas,
            geo_scheme=GEO_SCHEME,
            rights=RIGHTS,
            coverage_note=f"CIA World Factbook final edition (mirror {DATA_COMMIT[:7]}): every text field verbatim "
            f"(diplomatic contact sections excluded); {len(docs)} profiles; failed {len(failed)}.",
        )
        out[DS_TEXTS] = _coverage(reg)
    out["stats"] = dict(sorted(stats.items()))
    return out


async def ingest_disputes(run: Any, gecs: Iterable[str] | None = None) -> dict[str, Any]:
    got, failed = await _fetch_all(run, DISPUTES_PATHS, DISPUTES_COMMIT, gecs)
    stats: dict[str, int] = {}
    rows = Rows(DS_DISPUTES)
    for r, g, b, s in got:
        parse_disputes(b, r, g, s, rows, stats)
    out: dict[str, Any] = {"files": len(got), "failed": failed, "commit": DISPUTES_COMMIT, "stats": stats}
    if len(rows):
        reg = run.register(
            dataset=DS_DISPUTES,
            frame=rows.frame(),
            raw_sha256s=[s for *_, s in got],
            geo_scheme=GEO_SCHEME,
            rights=RIGHTS,
            coverage_note="CIA World Factbook 'Disputes - international' verbatim, mirror commit b1e7065 "
            f"(2024-04-18), the last edition with the field; {len(got)} profiles; failed {len(failed)}.",
        )
        out[DS_DISPUTES] = _coverage(reg)
    return out


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == NAME_DISPUTES:
        return await ingest_disputes(run)
    return await ingest_profiles(run)


__all__ = [
    "ATLAS_SOURCE",
    "COMMIT",
    "DISPUTES_COMMIT",
    "DS_DISPUTES",
    "DS_FIGURES",
    "DS_TEXTS",
    "NAME_DISPUTES",
    "NAME_PROFILES",
    "NUMERIC",
    "atlas_ingest",
    "atlas_source",
    "clean_text",
    "ingest_disputes",
    "ingest_profiles",
    "parse_borders",
    "parse_figures",
    "parse_number",
    "parse_shares",
    "parse_texts",
]
