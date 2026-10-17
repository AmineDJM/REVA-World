"""GDELT media-coverage signals -> atlas tables (V6, agent V6SIGNALS).

**Media-coverage signals, not event counts.** GDELT machine-codes news articles; every value here describes what
the monitored news media *published* (how many machine-coded event records, their CAMEO categories, Goldstein
scores and tone; how many articles carried a GKG theme). They are predictors for the forecast registry
(PRED-CONFLICT, PRED-FIN), never facts about events: a "riot" record is an article sentence the coder read as a
riot, possibly a duplicate, mis-geolocated or about an older event. Every row carries ``flags.signal_kind``.

Two atlas sources share the atlas key ``gdelt`` (one budget each, different hosts and politeness):

``atlas.gdelt_events`` -> table ``gdelt.events_cd`` (country-day)
    Source: the GDELT 2.0 Event Database 15-minute update files
    ``https://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.export.CSV.zip`` (tab-separated, 61 columns, no header;
    codebook ``GDELT-Event_Codebook-V2.0.pdf``). **Bounded sample**: one fixed update per UTC day (run option
    ``slot``, default ``120000`` = the update stamped 12:00:00 UTC), i.e. a 1/96 time-slot sample of the records
    GDELT added that day, for the last ``days`` days (default 730) ending ``end`` (default: yesterday UTC). One
    file is ~70-150 KB zipped, so the default window archives ~80 MB of raw bytes. Update files are immutable
    (the name is the update time), so a file already archived by this connector is reused from the archive
    (its own sha256 and retrieval time) instead of being downloaded again (run option ``refetch=1`` disables).

    Aggregation (method, exact): records are grouped by ``ActionGeo_CountryCode`` (FIPS 10-4 code of the place
    the coder located the action; ``provider_geo``; ``iso3`` only through the GeoNames FIPS crosswalk, never by
    name) and by the **update day** (``DATEADDED``, when GDELT first saw the record — the event-date field
    ``Day`` is ignored, so a row of day *d* only uses information published by the end of *d*). Per
    (country, day): ``records_all`` (number of event records), ``records_quad1..4`` (``QuadClass``),
    ``records_root01..20`` (``EventRootCode``), ``goldstein_mean`` and ``avgtone_mean`` (arithmetic mean over
    the records, rounded half-even to 4 decimals; ``flags.n`` = records averaged), ``mentions_sum`` and
    ``articles_sum`` (sums of ``NumMentions`` / ``NumArticles``). A count that is zero is **no row**; the day's
    ``_ALL`` row (``records_all`` over all records, ``records_no_actiongeo`` = records without a country) proves
    the update file was read, so a country absent on such a day had no coded record in the sampled update (an
    observed zero, not a missing value). A day whose update file is unavailable carries an ``_ALL`` row with no
    value and ``flags.missing`` (typed missing). Event URLs, actor names and places are never stored in the
    table (they stay in the archived raw file, which GDELT's terms allow to redistribute with a citation).

``atlas.gdelt_doc`` -> table ``gdelt.doc_theme_d`` (global, day)
    Source: the GDELT DOC 2.0 API timelines (``https://api.gdeltproject.org/api/v2/doc/doc``), one request per
    (GKG theme, mode): ``mode=timelinevolraw`` (``articles_<theme>`` = number of monitored articles carrying the
    theme that day; ``monitored_<theme>`` = the API's ``norm``, total articles monitored that day) and
    ``mode=timelinetone`` (``tone_<theme>`` = average tone of those articles), from ``doc_start`` (default
    2017-01-01) to ``end`` (default yesterday UTC), all monitored sources worldwide (``provider_geo``
    ``ALL_SOURCES``). Theme identifiers are checked against GDELT's own theme lookup file
    (``LOOKUP-GKGTHEMES.TXT``) before any timeline is requested. Days with ``norm`` 0 (no monitoring) are no
    row; tone is no row on a day without matching articles. The API is throttled ("one request every 5
    seconds"): the source spaces requests 10 s apart; a request still refused is reported, never filled.

Licence (quoted, checked against the archived about page at every run): "all datasets released by the GDELT
Project are available for unlimited and unrestricted use for any academic, commercial, or governmental use of any
kind without fee" ... "any use or redistribution of the data must include a citation to the GDELT Project and a
link to this website (https://www.gdeltproject.org/)". Every row carries ``flags.licence`` and
``flags.attribution``.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError, ValidationProblem

VERSION = "1.0.0"
ATLAS_SOURCE = "gdelt"
NAME_EVENTS = "atlas.gdelt_events"
NAME_DOC = "atlas.gdelt_doc"
SOURCE_KEY_EVENTS = "gdelt.events2"
SOURCE_KEY_DOC = "gdelt.doc2"
PARSER_EVENTS = "atlas_gdelt.events/1"
PARSER_DOC = "atlas_gdelt.doc/1"
DS_EVENTS = "events_cd"
DS_DOC = "doc_theme_d"
DATASETS_EVENTS = (DS_EVENTS,)
DATASETS_DOC = (DS_DOC,)
DATASETS = DATASETS_EVENTS + DATASETS_DOC

DATA_HOST = "data.gdeltproject.org"
API_HOST = "api.gdeltproject.org"
SITE_HOST = "gdeltproject.org"
TERMS_URL = f"https://{SITE_HOST}/about.html"
CODEBOOK_URL = f"https://{DATA_HOST}/documentation/GDELT-Event_Codebook-V2.0.pdf"  # quoted, not fetched per run
CAMEO_URL = f"https://{SITE_HOST}/data/lookups/CAMEO.eventcodes.txt"
THEMES_URL = f"https://{DATA_HOST}/api/v2/guides/LOOKUP-GKGTHEMES.TXT"
EXPORT_URL = "https://" + DATA_HOST + "/gdeltv2/{stamp}.export.CSV.zip"
DOC_URL = (
    "https://" + API_HOST + "/api/v2/doc/doc?query=theme:{theme}&mode={mode}&format=json"
    "&startdatetime={start}000000&enddatetime={end}235959"
)

SIGNAL_KIND = "media-coverage signal, not event counts"
GEO_SCHEME = "fips"
ALL_GEO = "_ALL"
DOC_GEO = "ALL_SOURCES"
FIRST_EXPORT_DAY = date(2015, 2, 19)  # GDELT 2.0 update files start 2015-02-18 23:00 (codebook: ID reset)
FIRST_DOC_DAY = date(2017, 1, 1)
DEFAULT_DAYS = 730
MAX_DAYS = 3650
DEFAULT_SLOT = "120000"
SLOT_RE = re.compile(r"^([01][0-9]|2[0-3])(00|15|30|45)00$")
EXPORT_COLS = 61
MAX_MALFORMED_SHARE = 0.01
MAX_MISSING_SHARE = 0.2
LICENCE = "GDELT Project terms of use: unlimited and unrestricted use, redistribution with citation and link"
ATTRIBUTION = "Source: The GDELT Project (https://www.gdeltproject.org/)."

# Column positions in a GDELT 2.0 export record (codebook V2.0, 0-based).
C_EVENTROOT, C_QUAD, C_GOLDSTEIN, C_MENTIONS, C_ARTICLES, C_TONE = 28, 29, 30, 31, 33, 34
C_ACTIONGEO_CC, C_DATEADDED = 53, 59

# Codebook definitions (verbatim, GDELT-Event_Codebook-V2.0.pdf, read 2026-10-17 at CODEBOOK_URL).
DEFINITIONS = {
    "QuadClass": "The entire CAMEO event taxonomy is ultimately organized under four primary classifications: "
    "Verbal Cooperation, Material Cooperation, Verbal Conflict, and Material Conflict. [...] 1=Verbal Cooperation, "
    "2=Material Cooperation, 3=Verbal Conflict, 4=Material Conflict.",
    "EventRootCode": "Similar to EventBaseCode, this defines the root-level category the event code falls under.",
    "GoldsteinScale": "Each CAMEO event code is assigned a numeric score from -10 to +10, capturing the theoretical "
    "potential impact that type of event will have on the stability of a country. [...] NOTE: this score is based "
    "on the type of event, not the specifics of the actual event record being recorded",
    "NumMentions": "This is the total number of mentions of this event across all source documents during the 15 "
    "minute update in which it was first seen.",
    "NumArticles": "This is the total number of source documents containing one or more mentions of this event "
    "during the 15 minute update in which it was first seen.",
    "AvgTone": "This is the average “tone” of all documents containing one or more mentions of this event "
    "during the 15 minute update in which it was first seen. The score ranges from -100 (extremely negative) to "
    "+100 (extremely positive).",
    "ActionGeo_CountryCode": "This is the 2-character FIPS10-4 country code for the location.",
    "DATEADDED": "This field stores the date the event was added to the master database in YYYYMMDDHHMMSS format "
    "in the UTC timezone.",
}  # fmt: skip
QUAD_LABELS = {"1": "Verbal Cooperation", "2": "Material Cooperation", "3": "Verbal Conflict", "4": "Material Conflict"}
# CAMEO root categories verbatim from GDELT's CAMEO.eventcodes.txt lookup (re-checked against it at every run).
CAMEO_ROOTS = {
    "01": "MAKE PUBLIC STATEMENT", "02": "APPEAL", "03": "EXPRESS INTENT TO COOPERATE", "04": "CONSULT",
    "05": "ENGAGE IN DIPLOMATIC COOPERATION", "06": "ENGAGE IN MATERIAL COOPERATION", "07": "PROVIDE AID",
    "08": "YIELD", "09": "INVESTIGATE", "10": "DEMAND", "11": "DISAPPROVE", "12": "REJECT", "13": "THREATEN",
    "14": "PROTEST", "15": "EXHIBIT FORCE POSTURE", "16": "REDUCE RELATIONS", "17": "COERCE", "18": "ASSAULT",
    "19": "FIGHT", "20": "USE UNCONVENTIONAL MASS VIOLENCE",
}  # fmt: skip

# GKG themes requested from the DOC API (identifiers as GDELT publishes them; checked against the lookup file).
THEMES: dict[str, str] = {
    "ARMEDCONFLICT": "conflict", "KILL": "conflict", "TERROR": "conflict", "PROTEST": "conflict",
    "MILITARY": "conflict", "REBELLION": "conflict", "UNREST_BELLIGERENT": "conflict", "CEASEFIRE": "conflict",
    "SANCTIONS": "conflict", "REFUGEES": "conflict", "FOOD_SECURITY": "society",
    "ECON_INFLATION": "finance", "ECON_INTEREST_RATES": "finance", "ECON_CENTRALBANK": "finance",
    "ECON_STOCKMARKET": "finance", "ECON_CURRENCY_EXCHANGE_RATE": "finance", "ECON_DEBT": "finance",
    "ECON_BANKRUPTCY": "finance", "ECON_OILPRICE": "finance", "ECON_TRADE_DISPUTE": "finance",
    "EPU_UNCERTAINTY": "finance", "EPU_CATS_MONETARY_POLICY": "finance",
}  # fmt: skip
DOC_MODES = ("timelinevolraw", "timelinetone")
DOC_RETRIES = 4  # extra attempts of a request refused with HTTP 429, after cooldown x attempt seconds
DOC_COOLDOWN_SECONDS = 60.0
DOC_GIVE_UP_AFTER = 6  # consecutive refused requests: stop asking (the rest is reported, never filled)

_Q_USE = (
    "all datasets released by the GDELT Project are available for unlimited and unrestricted use for any academic, "
    "commercial, or governmental use of any kind without fee."
)
_Q_REDIST = (
    "You may redistribute, rehost, republish, and mirror any of the GDELT datasets in any form. However, any use or "
    "redistribution of the data must include a citation to the GDELT Project and a link to this website "
    "(https://www.gdeltproject.org/)."
)
_RIGHTS_COMMON: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://www.gdeltproject.org/about.html#termsofuse",
    "terms_quote": _Q_USE + " " + _Q_REDIST,
    "attribution": ATTRIBUTION,
    "licence_checked": "2026-10-17",
    "provider": "The GDELT Project",
    "signal_kind": SIGNAL_KIND,
    "revision_policy": "not stated; value as fetched (as_of)",
    "policy_quotes": {"use": ("terms", _Q_USE), "redistribution": ("terms", _Q_REDIST)},
}
RIGHTS: dict[str, dict[str, Any]] = {
    DS_EVENTS: {
        **_RIGHTS_COMMON,
        "publication": "GDELT 2.0 update files every 15 minutes; the sampled update of day d is public at its stamp",
        "pub_lag": "same_day",
    },
    DS_DOC: {
        **_RIGHTS_COMMON,
        "publication": "DOC 2.0 API timelines, queried after the end of the UTC day (complete days only)",
        "pub_lag": "next_day",
    },
}

FILES_EVENTS: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("terms", TERMS_URL, DATASETS_EVENTS, ("text/html",), 2_000_000, role="policy"),
    hc.FileSpec("cameo", CAMEO_URL, DATASETS_EVENTS, ("text/plain",), 1_000_000, role="doc"),
)
FILES_DOC: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("terms", TERMS_URL, DATASETS_DOC, ("text/html",), 2_000_000, role="policy"),
    hc.FileSpec("themes", THEMES_URL, DATASETS_DOC, ("text/plain",), 10_000_000, role="doc"),
)


def code_digest() -> str:
    return hc.code_digest(sys.modules[__name__])


def atlas_source() -> list[AtlasSourceSpec]:
    digest = code_digest()
    return [
        AtlasSourceSpec(
            name=NAME_EVENTS,
            version=VERSION,
            source_key=SOURCE_KEY_EVENTS,
            atlas_source=ATLAS_SOURCE,
            provider="The GDELT Project (GDELT 2.0 Event Database, 15-minute update files)",
            licence=LICENCE,
            allowed_hosts=(DATA_HOST, SITE_HOST),
            datasets=DATASETS_EVENTS,
            dataset_ids=("GDELT2:events.export",),
            cadence_seconds=hc.DAY,
            planned_requests=DEFAULT_DAYS + len(FILES_EVENTS),
            max_response_bytes=10_000_000,
            min_interval_seconds=1.0,
            parser_version=PARSER_EVENTS,
            code_digest=digest,
            field_mappings={
                "geo_scheme": GEO_SCHEME,
                "tables": [f"{ATLAS_SOURCE}.{DS_EVENTS}"],
                "signal_kind": SIGNAL_KIND,
            },
            notes="one 15-minute export update per day (~110 KB) for the last 730 days (~80 MB raw on the first "
            "run; archived update files are reused) + about page + CAMEO code lookup",
        ),
        AtlasSourceSpec(
            name=NAME_DOC,
            version=VERSION,
            source_key=SOURCE_KEY_DOC,
            atlas_source=ATLAS_SOURCE,
            provider="The GDELT Project (DOC 2.0 API timelines)",
            licence=LICENCE,
            allowed_hosts=(API_HOST, DATA_HOST, SITE_HOST),
            datasets=DATASETS_DOC,
            dataset_ids=("GDELT:DOC2.timelinevolraw", "GDELT:DOC2.timelinetone"),
            cadence_seconds=hc.DAY,
            planned_requests=len(THEMES) * len(DOC_MODES) + len(FILES_DOC),
            max_response_bytes=10_000_000,
            min_interval_seconds=10.0,
            parser_version=PARSER_DOC,
            code_digest=digest,
            field_mappings={"tables": [f"{ATLAS_SOURCE}.{DS_DOC}"], "signal_kind": SIGNAL_KIND},
            notes=f"{len(THEMES)} GKG themes x 2 timeline modes (~0.2-0.3 MB each) + about page + theme lookup",
        ),
    ]


# ----------------------------------------------------------------------------- run options
@dataclass(frozen=True)
class Window:
    start: date
    end: date

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]


def _day_opt(options: Mapping[str, Any], key: str) -> date | None:
    v = options.get(key)
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v))
    except ValueError as e:
        raise ValidationProblem(f"run option {key} must be YYYY-MM-DD", option=key) from e


def events_window(options: Mapping[str, Any], today: date) -> tuple[Window, str]:
    end = _day_opt(options, "end") or today - timedelta(days=1)
    if end >= today:
        raise ValidationProblem("run option end must be before the run day (complete days only)", end=str(end))
    try:
        days = int(options.get("days", DEFAULT_DAYS))
    except (TypeError, ValueError) as e:
        raise ValidationProblem("run option days must be an integer") from e
    if not 1 <= days <= MAX_DAYS:
        raise ValidationProblem(f"run option days must be 1..{MAX_DAYS}", days=days)
    slot = str(options.get("slot", DEFAULT_SLOT))
    if not SLOT_RE.match(slot):
        raise ValidationProblem("run option slot must be HHMMSS on a 15-minute boundary", slot=slot)
    start = max(end - timedelta(days=days - 1), FIRST_EXPORT_DAY)
    if start > end:
        raise ValidationProblem("events window ends before the first GDELT 2.0 update", end=str(end))
    return Window(start, end), slot


def doc_window(options: Mapping[str, Any], today: date) -> tuple[Window, tuple[str, ...]]:
    end = _day_opt(options, "end") or today - timedelta(days=1)
    start = max(_day_opt(options, "doc_start") or FIRST_DOC_DAY, FIRST_DOC_DAY)
    if end >= today or start > end:
        raise ValidationProblem("DOC window must be complete days before the run day", start=str(start), end=str(end))
    raw = options.get("themes")
    themes = tuple(t.strip() for t in str(raw).split(",") if t.strip()) if raw else tuple(THEMES)
    unknown = sorted(set(themes) - set(THEMES))
    if unknown:
        raise ValidationProblem("run option themes: unknown theme (declare it in THEMES first)", unknown=unknown)
    return Window(start, end), themes


def export_url(day: date, slot: str) -> str:
    return EXPORT_URL.format(stamp=f"{day:%Y%m%d}{slot}")


def doc_url(theme: str, mode: str, w: Window) -> str:
    return DOC_URL.format(theme=theme, mode=mode, start=f"{w.start:%Y%m%d}", end=f"{w.end:%Y%m%d}")


# ----------------------------------------------------------------------------- events: parsing
def _dec(text: str) -> Decimal | None:
    v, _ = hc.number(text)
    return Decimal(v) if v is not None else None


def _mean(total: Decimal, n: int) -> str:
    q = (total / Decimal(n)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
    s = format(q, "f")
    return "0.0000" if s == "-0.0000" else s


@dataclass
class _Agg:
    n: int = 0
    quad: Counter[str] = field(default_factory=Counter)
    root: Counter[str] = field(default_factory=Counter)
    gold_sum: Decimal = Decimal(0)
    gold_n: int = 0
    tone_sum: Decimal = Decimal(0)
    tone_n: int = 0
    mentions: int = 0
    mentions_n: int = 0
    articles: int = 0
    articles_n: int = 0


@dataclass
class SlotAgg:
    """Aggregates of one 15-minute update file (one UTC day of the sample)."""

    stamp: str
    records: int = 0
    malformed: int = 0
    no_geo: int = 0
    dateadded_mismatch: int = 0
    unknown_quad: int = 0
    unknown_root: int = 0
    by_geo: dict[str, _Agg] = field(default_factory=lambda: defaultdict(_Agg))


def _int(text: str) -> int | None:
    t = text.strip()
    return int(t) if t.isdigit() else None


def parse_export(body: bytes, stamp: str) -> SlotAgg:
    """Aggregate one GDELT 2.0 export update (zip with one ``<stamp>.export.CSV`` member) by ActionGeo country."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise hc.err(f"{stamp}: not a zip archive", kind="decoding_error") from e
    names = zf.namelist()
    want = f"{stamp}.export.CSV"
    if names != [want]:
        raise hc.err(f"{stamp}: archive members {names[:3]} != [{want}]")
    info = zf.getinfo(want)
    if info.file_size > 200_000_000:
        raise hc.err(f"{stamp}: member of {info.file_size} bytes", kind="invalid")
    text = zf.read(want).decode("utf-8", "replace")
    agg = SlotAgg(stamp)
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line:
            continue
        agg.records += 1
        c = line.split("\t")
        if len(c) != EXPORT_COLS:
            agg.malformed += 1
            continue
        if c[C_DATEADDED] != stamp:
            agg.dateadded_mismatch += 1
        geo = c[C_ACTIONGEO_CC].strip()
        if not geo:
            agg.no_geo += 1
            continue
        if not re.fullmatch(r"[A-Z]{2}", geo):
            agg.malformed += 1
            continue
        a = agg.by_geo[geo]
        a.n += 1
        q = c[C_QUAD].strip()
        if q in QUAD_LABELS:
            a.quad[q] += 1
        else:
            agg.unknown_quad += 1
        r = c[C_EVENTROOT].strip()
        if r in CAMEO_ROOTS:
            a.root[r] += 1
        else:
            agg.unknown_root += 1
        g = _dec(c[C_GOLDSTEIN])
        if g is not None:
            a.gold_sum += g
            a.gold_n += 1
        t = _dec(c[C_TONE])
        if t is not None:
            a.tone_sum += t
            a.tone_n += 1
        m = _int(c[C_MENTIONS])
        if m is not None:
            a.mentions += m
            a.mentions_n += 1
        n_art = _int(c[C_ARTICLES])
        if n_art is not None:
            a.articles += n_art
            a.articles_n += 1
    if agg.records and agg.malformed > MAX_MALFORMED_SHARE * agg.records:
        raise hc.err(f"{stamp}: {agg.malformed}/{agg.records} malformed records (column layout changed?)")
    return agg


_EVENT_SAMPLING = (
    "GDELT 2.0 Event export, one 15-minute update per UTC day (a fixed 1/96 time-slot sample of the records GDELT "
    "added that day)"
)


def add_slot_rows(rows: hc.DailyRows, agg: SlotAgg, day: date, f: hc.Fetched, slot: str, *, reused: bool) -> None:
    rights = RIGHTS[DS_EVENTS]
    common = {
        "signal_kind": SIGNAL_KIND,
        "attribution": ATTRIBUTION,
        "sampling": _EVENT_SAMPLING,
        "slot_utc": slot,
        "day_basis": "DATEADDED (update day), not the event date",
        "reused_archive": True if reused else None,
    }
    sel = {"file": f.key, "member": f"{agg.stamp}.export.CSV"}

    def add(geo: str, ind: str, value: str, unit: str, *, n: int | None = None, basis: str, col: str) -> None:
        fl = rows.flags(**hc.base_flags(rights, f, **common, n=n, basis=basis,
                                        geo_basis=None if geo == ALL_GEO else "ActionGeo_CountryCode (FIPS 10-4)"))  # fmt: skip
        s = {**sel, "column": col, **({} if geo == ALL_GEO else {"where": f"ActionGeo_CountryCode={geo}"})}
        rows.add(indicator=ind, provider_geo=geo, day=day, value=value, unit=unit, flags=fl, raw_sha256=f.sha256,
                 selector=s)  # fmt: skip

    add(ALL_GEO, "records_all", str(agg.records), "event records", basis="all records of the update file",
        col="*")  # fmt: skip
    add(ALL_GEO, "records_no_actiongeo", str(agg.no_geo), "event records",
        basis="records with an empty ActionGeo_CountryCode", col="ActionGeo_CountryCode")  # fmt: skip
    for geo in sorted(agg.by_geo):
        a = agg.by_geo[geo]
        add(geo, "records_all", str(a.n), "event records", basis="records located in the country", col="*")
        for q in sorted(a.quad):
            add(geo, f"records_quad{q}", str(a.quad[q]), "event records",
                basis=f"QuadClass={q} ({QUAD_LABELS[q]})", col="QuadClass")  # fmt: skip
        for r in sorted(a.root):
            add(geo, f"records_root{r}", str(a.root[r]), "event records",
                basis=f"EventRootCode={r} (CAMEO {CAMEO_ROOTS[r]})", col="EventRootCode")  # fmt: skip
        if a.gold_n:
            add(geo, "goldstein_mean", _mean(a.gold_sum, a.gold_n), "Goldstein scale points (-10..+10)",
                n=a.gold_n, basis="mean GoldsteinScale over the records, half-even to 4 decimals",
                col="GoldsteinScale")  # fmt: skip
        if a.tone_n:
            add(geo, "avgtone_mean", _mean(a.tone_sum, a.tone_n), "tone score (-100..+100)", n=a.tone_n,
                basis="mean AvgTone over the records, half-even to 4 decimals", col="AvgTone")  # fmt: skip
        if a.mentions_n and a.mentions:
            add(geo, "mentions_sum", str(a.mentions), "mentions", n=a.mentions_n, basis="sum of NumMentions",
                col="NumMentions")  # fmt: skip
        if a.articles_n and a.articles:
            add(geo, "articles_sum", str(a.articles), "source documents", n=a.articles_n,
                basis="sum of NumArticles", col="NumArticles")  # fmt: skip


def add_missing_slot(rows: hc.DailyRows, day: date, slot: str, reason: str, as_of: str, terms: hc.Fetched) -> None:
    """Typed missing: the day's update file could not be read (no invented zero)."""
    rights = RIGHTS[DS_EVENTS]
    fl = rows.flags(licence=rights["licence"], as_of=as_of, precision="day", estimate_kind="observation",
                    signal_kind=SIGNAL_KIND, missing=reason, slot_utc=slot, sampling=_EVENT_SAMPLING,
                    attribution=ATTRIBUTION)  # fmt: skip
    c = rows.cols
    k = ("records_all", ALL_GEO, day)
    if k in rows._seen:
        raise hc.err(f"{DS_EVENTS}: duplicate cell {k}", kind="invalid")
    rows._seen.add(k)
    for col, v in (("indicator", "records_all"), ("iso3", None), ("provider_geo", ALL_GEO), ("period_start", day),
                   ("period_end", day + timedelta(days=1)), ("value", None), ("value_text", None),
                   ("unit", "event records"), ("flags", fl), ("raw_sha256", terms.sha256),
                   ("selector", hc.canon({"file": "terms", "missing_update": export_url(day, slot)}))):  # fmt: skip
        c[col].append(v)


# ----------------------------------------------------------------------------- DOC API: parsing
def _doc_json(body: bytes, what: str) -> dict[str, Any]:
    try:
        d = json.loads(body.decode("utf-8-sig"), parse_float=str, parse_int=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise hc.err(f"{what}: not JSON ({body[:80]!r})", kind="malformed_json") from e
    if not isinstance(d, dict) or not isinstance(d.get("timeline"), list):
        raise hc.err(f"{what}: no timeline")
    res = (d.get("query_details") or {}).get("date_resolution")
    if res != "day":
        raise hc.err(f"{what}: date_resolution {res!r} (day expected; shorten the window)")
    if len(d["timeline"]) != 1:
        raise hc.err(f"{what}: {len(d['timeline'])} series (1 expected)")
    return d


def _doc_day(s: Any) -> date:
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T000000Z", str(s))
    if not m:
        raise hc.err(f"DOC timeline date {s!r}")
    return date(int(m[1]), int(m[2]), int(m[3]))


def parse_doc(
    rows: hc.DailyRows,
    theme: str,
    vol: hc.Fetched | None,
    tone: hc.Fetched | None,
    w: Window,
    report: dict[str, Any],
) -> None:
    rights = RIGHTS[DS_DOC]
    common = {"signal_kind": SIGNAL_KIND, "attribution": ATTRIBUTION, "gkg_theme": theme,
              "scope": "all sources monitored by the GDELT DOC 2.0 API (worldwide, all languages)"}  # fmt: skip
    rep: dict[str, Any] = {"articles": 0, "tone": 0, "zero_norm_days": 0, "outside_window": 0}
    with_articles: set[date] = set()
    if vol is not None:
        d = _doc_json(vol.body, f"{theme}/timelinevolraw")
        series = d["timeline"][0]
        for i, p in enumerate(series.get("data") or []):
            day = _doc_day(p.get("date"))
            if not w.start <= day <= w.end:
                rep["outside_window"] += 1
                continue
            v, _ = hc.number(p.get("value"))
            norm, _ = hc.number(p.get("norm"))
            if v is None or norm is None or not re.fullmatch(r"\d+", v) or not re.fullmatch(r"\d+", norm):
                raise hc.err(f"{theme}/timelinevolraw: point {i} is not a pair of counts: {p}")
            if norm == "0":
                rep["zero_norm_days"] += 1
                continue
            sel = {"file": vol.key, "series": series.get("series"), "point": i, "date": p.get("date")}
            fl = rows.flags(**hc.base_flags(rights, vol, **common, series=series.get("series"),
                                            api_field="value", mode="timelinevolraw"))  # fmt: skip
            rows.add(indicator=f"articles_{theme.lower()}", provider_geo=DOC_GEO, day=day, value=v,
                     unit="articles", flags=fl, raw_sha256=vol.sha256, selector={**sel, "field": "value"})  # fmt: skip
            fl = rows.flags(**hc.base_flags(rights, vol, **common, series=series.get("series"), api_field="norm",
                                            mode="timelinevolraw", basis="total articles monitored that day"))  # fmt: skip
            rows.add(indicator=f"monitored_{theme.lower()}", provider_geo=DOC_GEO, day=day, value=norm,
                     unit="articles", flags=fl, raw_sha256=vol.sha256, selector={**sel, "field": "norm"})  # fmt: skip
            rep["articles"] += 1
            if v != "0":
                with_articles.add(day)
    if tone is not None:
        d = _doc_json(tone.body, f"{theme}/timelinetone")
        series = d["timeline"][0]
        for i, p in enumerate(series.get("data") or []):
            day = _doc_day(p.get("date"))
            if not w.start <= day <= w.end:
                continue
            if vol is not None and day not in with_articles:
                continue  # no matching article that day (or no monitoring): an average of nothing is no value
            v, normalised = hc.number(p.get("value"))
            if v is None:
                raise hc.err(f"{theme}/timelinetone: point {i} value {p.get('value')!r}")
            fl = rows.flags(**hc.base_flags(rights, tone, **common, series=series.get("series"), api_field="value",
                                            mode="timelinetone", value_normalised=normalised or None,
                                            articles_check=None if vol is not None else "volume timeline unavailable"))  # fmt: skip
            rows.add(indicator=f"tone_{theme.lower()}", provider_geo=DOC_GEO, day=day, value=v,
                     unit="tone score (-100..+100)", flags=fl, raw_sha256=tone.sha256,
                     selector={"file": tone.key, "series": series.get("series"), "point": i, "date": p.get("date"),
                               "field": "value"})  # fmt: skip
            rep["tone"] += 1
    report.setdefault("themes", {})[theme] = rep


def check_cameo(lookup: hc.Fetched) -> dict[str, str | None]:
    """Root codes whose label differs from GDELT's CAMEO lookup (``code<TAB>label`` lines): ``{code: their label}``."""
    listed = {}
    for ln in lookup.text("utf-8").splitlines():
        code, sep, label = ln.partition("\t")
        if sep:
            listed[code.strip()] = label.strip()
    return {c: listed.get(c) for c, lab in CAMEO_ROOTS.items() if listed.get(c) != lab}


def check_themes(lookup: hc.Fetched, themes: Sequence[str]) -> dict[str, bool]:
    """``{theme: listed?}`` against GDELT's theme lookup (``THEME<TAB>count`` lines)."""
    listed = {ln.split("\t", 1)[0].strip() for ln in lookup.text("utf-8").splitlines() if "\t" in ln}
    return {t: t in listed for t in themes}


# ----------------------------------------------------------------------------- fetching
def _reuse_archived(run: Any, url: str, key: str) -> hc.Fetched | None:
    """An immutable GDELT update file this connector already archived live (same request fingerprint)."""
    if getattr(run, "engine", None) is None or str((run.options or {}).get("refetch", "")) in ("1", "true", "yes"):
        return None
    from sqlalchemy import text

    from reva.adapters.http.safe_client import SECRET_QUERY_PARAMS, request_fingerprint

    fp = request_fingerprint("GET", url, secret_params=tuple(SECRET_QUERY_PARAMS))
    with run.engine.connect() as c:
        row = c.execute(
            text(
                "SELECT a.raw_sha256, a.retrieved_at FROM evidence.acquisition a "
                "JOIN evidence.connector_version v ON v.id = a.connector_version_id "
                "WHERE a.request_fingerprint = :fp AND a.origin = :origin AND v.name = :name "
                "ORDER BY a.retrieved_at DESC LIMIT 1"
            ),
            {"fp": fp, "origin": run.origin, "name": run.spec.name},
        ).first()
    if row is None:
        return None
    body = run.store.get("raw", row[0])
    run.archived.append(row[0])
    return hc.Fetched(key, url, body, row[0], row[1])


async def _try_fetch(run: Any, spec: hc.FileSpec) -> tuple[hc.Fetched | None, str | None]:
    try:
        return await hc.fetch(run, spec), None
    except SourceResponseError as e:
        status = e.details.get("status")
        return None, f"HTTP {status}" if status else (e.details.get("kind") or type(e).__name__)


async def ingest_events(run: Any) -> dict[str, Any]:
    opts = dict(run.options or {})
    w, slot = events_window(opts, datetime.now(UTC).date() if run.origin == "live" else _fixture_today(opts))
    pages = await hc.fetch_all(run, FILES_EVENTS, DATASETS_EVENTS)
    drift = check_cameo(pages["cameo"])
    if drift:
        raise hc.err(f"CAMEO root labels differ from GDELT's lookup: {drift}")
    rows = hc.DailyRows(ATLAS_SOURCE, DS_EVENTS)
    rep: dict[str, Any] = {"window": [str(w.start), str(w.end)], "slot_utc": slot, "fetched": 0, "reused": 0,
                           "missing": {}, "records": 0, "malformed": 0, "no_actiongeo": 0, "dateadded_mismatch": 0,
                           "unknown_quad": 0, "unknown_root": 0, "geos": Counter()}  # fmt: skip
    for day in w.days():
        stamp = f"{day:%Y%m%d}{slot}"
        url = export_url(day, slot)
        key = f"export_{stamp}"
        f = _reuse_archived(run, url, key)
        reused = f is not None
        why = None
        if f is None:
            f, why = await _try_fetch(run, hc.FileSpec(key, url, DATASETS_EVENTS,
                                                       ("application/zip", "application/octet-stream"), 10_000_000))  # fmt: skip
        if f is None:
            rep["missing"][stamp] = why
            add_missing_slot(rows, day, slot, f"GDELT update file not available ({why})", pages["terms"].as_of,
                             pages["terms"])  # fmt: skip
            continue
        rep["reused" if reused else "fetched"] += 1
        agg = parse_export(f.body, stamp)
        add_slot_rows(rows, agg, day, f, slot, reused=reused)
        for k in ("records", "malformed", "dateadded_mismatch", "unknown_quad", "unknown_root"):
            rep[k] += getattr(agg, k)
        rep["no_actiongeo"] += agg.no_geo
        rep["geos"].update(agg.by_geo.keys())
    n_days = len(w.days())
    if len(rep["missing"]) > max(2, MAX_MISSING_SHARE * n_days):
        raise hc.err(f"{DS_EVENTS}: {len(rep['missing'])}/{n_days} update files unavailable", kind="incomplete")
    rep["geos"] = len(rep["geos"])
    rep["policy_quotes_found"] = hc.check_policy_pages(pages, FILES_EVENTS, RIGHTS, DATASETS_EVENTS)
    tables = hc.register_tables(run, {DS_EVENTS: hc.TableBuild(rows)}, rights=RIGHTS, notes=NOTES,
                                source_key=SOURCE_KEY_EVENTS, geo_scheme=GEO_SCHEME)  # fmt: skip
    return {"ok": True, "tables": tables, "report": {DS_EVENTS: rep}}


async def ingest_doc(run: Any) -> dict[str, Any]:
    opts = dict(run.options or {})
    w, themes = doc_window(opts, datetime.now(UTC).date() if run.origin == "live" else _fixture_today(opts))
    pages = await hc.fetch_all(run, FILES_DOC, DATASETS_DOC)
    listed = check_themes(pages["themes"], themes)
    if not all(listed.values()):
        raise hc.err(f"themes not in GDELT's lookup file: {[t for t, ok in listed.items() if not ok]}")
    rows = hc.DailyRows(ATLAS_SOURCE, DS_DOC)
    report: dict[str, Any] = {"window": [str(w.start), str(w.end)], "failed": {}, "throttle_waits": 0}
    retries = int(opts.get("doc_retries", DOC_RETRIES)) if run.origin == "live" else 0
    cooldown = float(opts.get("doc_cooldown", DOC_COOLDOWN_SECONDS))
    if not (0 <= retries <= 10 and 0 <= cooldown <= 900):
        raise ValidationProblem("run options doc_retries must be 0..10 and doc_cooldown 0..900 seconds")
    consecutive = 0
    for theme in themes:
        got: dict[str, hc.Fetched | None] = {}
        for mode in DOC_MODES:
            key = f"{theme}/{mode}"
            if consecutive >= DOC_GIVE_UP_AFTER:
                got[mode] = None
                report["failed"][key] = f"not requested: {consecutive} consecutive requests refused (API throttling)"
                continue
            spec = hc.FileSpec(f"doc_{theme}_{mode}", doc_url(theme, mode, w), DATASETS_DOC,
                               ("application/json",), 10_000_000)  # fmt: skip
            for attempt in range(retries + 1):
                got[mode], why = await _try_fetch(run, spec)
                if why != "HTTP 429" or attempt == retries:
                    break
                report["throttle_waits"] += 1  # GDELT: "Please limit requests to one every 5 seconds"
                await asyncio.sleep(cooldown * (attempt + 1))
            consecutive = 0 if got[mode] is not None else consecutive + 1
            if why:
                report["failed"][key] = why
        if got["timelinevolraw"] is None and got["timelinetone"] is None:
            continue
        parse_doc(rows, theme, got["timelinevolraw"], got["timelinetone"], w, report)
    if not len(rows):
        raise hc.err(f"{DS_DOC}: no timeline could be read ({report['failed']})", kind="incomplete")
    report["policy_quotes_found"] = hc.check_policy_pages(pages, FILES_DOC, RIGHTS, DATASETS_DOC)
    tables = hc.register_tables(run, {DS_DOC: hc.TableBuild(rows)}, rights=RIGHTS, notes=NOTES,
                                source_key=SOURCE_KEY_DOC)  # fmt: skip
    return {"ok": True, "tables": tables, "report": {DS_DOC: report}}


def _fixture_today(opts: Mapping[str, Any]) -> date:
    """Fixture runs are pinned to the capture: the day after ``end`` (default 2026-10-16)."""
    end = _day_opt(opts, "end") or date(2026, 10, 16)
    return end + timedelta(days=1)


NOTES = {
    DS_EVENTS: (
        "MEDIA-COVERAGE SIGNAL, NOT EVENT COUNTS. GDELT 2.0 machine-coded event records from news articles, sampled "
        "as one 15-minute update per UTC day (flags.slot_utc; 1/96 of the records added that day), aggregated by "
        "ActionGeo_CountryCode (FIPS 10-4, iso3 via the GeoNames FIPS column) and update day (DATEADDED, not the "
        "event date): records_all, records_quad1-4 (QuadClass), records_root01-20 (CAMEO EventRootCode), "
        "goldstein_mean and avgtone_mean (mean over flags.n records, 4 decimals), mentions_sum, articles_sum. "
        "Zero counts are no row; provider_geo _ALL carries the day's total records (proof the file was read) or "
        "flags.missing when the update file was unavailable. Use as predictors only. GDELT terms: free use, "
        "citation + link to https://www.gdeltproject.org/ required."
    ),
    DS_DOC: (
        "MEDIA-COVERAGE SIGNAL, NOT EVENT COUNTS. GDELT DOC 2.0 API daily timelines for selected GKG themes over all "
        "monitored sources worldwide (provider_geo ALL_SOURCES): articles_<theme> (articles carrying the theme), "
        "monitored_<theme> (the API's norm: all articles monitored that day), tone_<theme> (average tone). Days "
        "with no monitoring are no row. flags.as_of = fetch date (the API may change past values). Use as "
        "predictors only. GDELT terms: free use, citation + link to https://www.gdeltproject.org/ required."
    ),
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == NAME_EVENTS:
        return await ingest_events(run) if run.wants(DS_EVENTS) else {"ok": True, "tables": []}
    if run.spec.name == NAME_DOC:
        return await ingest_doc(run) if run.wants(DS_DOC) else {"ok": True, "tables": []}
    raise ValidationProblem("unknown GDELT atlas source", source=run.spec.name)


__all__ = [
    "ATLAS_SOURCE",
    "DATASETS",
    "NAME_DOC",
    "NAME_EVENTS",
    "RIGHTS",
    "SIGNAL_KIND",
    "THEMES",
    "atlas_ingest",
    "atlas_source",
    "parse_doc",
    "parse_export",
]
