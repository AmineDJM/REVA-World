"""UN peacekeeping: uniformed personnel contributions, fatalities and missions (agent A-MILITARY).

Source and access
-----------------
The **UN Peace and Security Data Hub** (DPPA-DPO Information Management Unit, ``psdata.un.org``) serves its
datasets as CSV from its public API ``https://api.psdata.un.org/public/data/<dataset id>/csv`` (one request
per whole dataset, no key). The Hub publishes the same datasets on HDX (organisation ``unpeacesecurity``)
with an explicit dataset licence: **CC BY 3.0 IGO** (``license_id: cc-by-igo``). Every run reads the HDX
record of each dataset (``data.humdata.org/api/3/action/package_show``) and refuses to ingest when the
licence or the publishing organisation changed (nothing archived, nothing registered). The Hub web site's
footer links the generic UN web-site terms; the dataset-level licence on the publisher's HDX records is the
more specific statement and is the one recorded (maintainer review pending; see docs/dev/v3_a_military.md).

Datasets (``FILES``)
--------------------
* ``monthly`` -- DPO-UCHISTORICAL "Historical Uniformed Personnel Contribution": contributing country x
  mission x personnel type (Troops, Staff Officer, Experts on Mission, Individual Police, Formed Police
  Units) x month-end reporting date, male and female counts, 2010-01 -> latest.
* ``pre2010`` -- DPPADPO-UCPre2010 (archived): contributor x mission x month-end date, 1990-11 -> 2009-12,
  legacy categories Troops / Civilian_Police / Observers and, from late 2009 only, the new categories
  Individual_Police, Formed_Police_Units, Experts_on_Mission **which overlap the legacy ones**
  (Civilian_Police = Individual_Police + Formed_Police_Units, Observers = Experts_on_Mission); also the
  provider's ``Mission_Country`` of each mission.
* ``annual`` -- DataCommons-UCHISTORICAL: provider **annual averages** per country x personnel type x sex,
  2010 -> current year (the current year is incomplete at retrieval and flagged).
* ``fatalities`` -- DataCommons-Fatalities: fatalities per incident year x mission x casualty nationality
  x type of fatality, 1948 -> latest (nationality of the casualties, not the incident location).
* ``pko`` -- DPPADPOSS-PKO: every past and present peacekeeping operation (acronym, name, start, mandate
  end, HQ location, HQ coordinates as published: decimal comma, several rows with latitude/longitude
  apparently swapped -- kept verbatim, never corrected).
* ``fieldmissions`` -- DPPA-FieldMissions-Missions: current/recent field missions with ``location``,
  initial mandate symbol and lead entity.

Outputs
-------
Atlas tables (source ``unpk``): ``contrib_monthly``, ``contrib_pre2010``, ``contrib_annual``, ``fatalities``
(values verbatim, one row per reported cell; empty cell -> no row; a reported 0 is kept) and ``totals``
(**computed** by REVA, flagged ``computed_from``: per country and date / year, sums that avoid the
pre-2010 category overlap). KG (source key ``un.psdata``): mission entities ``unpk:mission:<acronym>``
and statements ``unpk:contributed_personnel_to`` (country -> mission, per calendar year, first/last
reporting date), ``unpk:mission_country`` (mission -> country as reported by the provider),
``unpk:start_date`` / ``unpk:end_date`` / ``unpk:hq_location`` / ``unpk:initial_mandate`` /
``unpk:lead_entity``.

Countries
---------
Provider codes are ISO 3166 alpha-3 (scheme ``unpk`` in ``registry/atlas/geo_aliases/a_military.yaml``).
The provider re-codes dissolved states with their successors' codes (``RUS`` from 1990-11, ``CZE`` from
1990-11, ``SRB`` for Yugoslavia / Serbia and Montenegro, ``YUG``/``SCG`` in the fatalities): rows dated
while the predecessor existed are **kept unmapped** (``iso3 = null``) with the predecessor's Wikidata QID
from ``registry/entities/historical_states_qids.json`` (``HISTORICAL_WINDOWS``); a period that straddles a
dissolution gets no country and no QID (flagged). Nothing is guessed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import (
    AtlasRows,
    KgEntity,
    KgStatement,
    atlas_frame,
    kg_available,
    load_kg,
    load_qid_for_iso3,
)
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import ValidationProblem

SOURCE_KEY = "un.psdata"
CONNECTOR_NAME = "atlas.peacekeeping"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas_peacekeeping/1"
ATLAS_SOURCE = "unpk"
GEO_SCHEME = "unpk"
LOCATION_SCHEME = "unpk_location"
API_HOST = "api.psdata.un.org"
HDX_HOST = "data.humdata.org"
API_URL = "https://" + API_HOST + "/public/data/{id}/csv"
HDX_URL = "https://" + HDX_HOST + "/api/3/action/package_show?id={name}"
EXPECTED_LICENCE_ID = "cc-by-igo"
EXPECTED_ORGANISATION = "unpeacesecurity"
MAX_BYTES = 80_000_000

RIGHTS: dict[str, Any] = {
    "licence": "CC BY 3.0 IGO",
    "licence_url": "http://creativecommons.org/licenses/by/3.0/igo/legalcode",
    "licence_evidence": "HDX dataset records of the UN Peace and Security Data Hub (organisation "
    "unpeacesecurity), license_id cc-by-igo, checked on every run",
    "attribution": "United Nations Peace and Security Data Hub (DPPA-DPO), https://psdata.un.org",
    "provider": "United Nations Department of Peace Operations / Department of Political and Peacebuilding Affairs",
    "source_key": SOURCE_KEY,
}

#: key -> (Hub dataset id, HDX package name, minimum rows in a live run, required columns)
FILES: dict[str, tuple[str, str, int, tuple[str, ...]]] = {
    "monthly": (
        "DPO-UCHISTORICAL",
        "peacekeeping-uniformed-contributions-by-gender",
        100_000,
        (
            "contributing_country",
            "isocode3",
            "mission_acronym",
            "personnel_type",
            "male_personnel",
            "female_personnel",
            "last_reporting_date",
        ),
    ),
    "pre2010": (
        "DPPADPO-UCPre2010",
        "dppadpo-ucpre2010",
        50_000,
        (
            "Date",
            "Contributor",
            "Contributor_ISO-3",
            "Mission",
            "Mission_Country",
            "Mission_Country_ISO-3",
            "Experts_on_Mission",
            "Formed_Police_Units",
            "Individual_Police",
            "Civilian_Police",
            "Troops",
            "Observers",
        ),
    ),
    "annual": (
        "DataCommons-UCHISTORICAL",
        "datacommons-uchistorical",
        3_000,
        ("Year", "Contributing_Country", "ISOCode3", "Personnel_Type", "Male", "Female"),
    ),
    "fatalities": (
        "DataCommons-Fatalities",
        "datacommons-fatalities",
        2_000,
        ("Incident_Year", "mission_id", "Mission", "Casualty_Nationality", "Type_of_Fatalities", "ISOCode3", "Fatalities"),
    ),
    "pko": (
        "DPPADPOSS-PKO",
        "peacekeeping-core-dataset-active-missions",
        60,
        (
            "mission_id",
            "mission_acronym",
            "mission_name",
            "mission_isactive",
            "start_date",
            "end_date",
            "mission_location",
            "mission_longitude",
            "mission_latitude",
            "last_update",
        ),
    ),
    "fieldmissions": (
        "DPPA-FieldMissions-Missions",
        "dppa-fieldmissions-missions",
        20,
        ("missions", "mission_name", "location", "initial_symbol", "lead_entity", "open_closed", "last_update"),
    ),
}

DATASETS: tuple[str, ...] = ("contrib_monthly", "contrib_pre2010", "contrib_annual", "fatalities", "totals")
#: atlas dataset -> raw files it is parsed from
DATASET_FILES: dict[str, tuple[str, ...]] = {
    "contrib_monthly": ("monthly",),
    "contrib_pre2010": ("pre2010",),
    "contrib_annual": ("annual",),
    "fatalities": ("fatalities",),
    "totals": ("monthly", "pre2010", "annual", "fatalities"),
}
KG_FILES: tuple[str, ...] = ("monthly", "pre2010", "pko", "fieldmissions")

PRE2010_LEGACY = ("Troops", "Civilian_Police", "Observers")  # non-overlapping, 1990-2009
PRE2010_NEW = ("Individual_Police", "Formed_Police_Units", "Experts_on_Mission")  # late 2009, overlap legacy
PRE2010_CATEGORIES = PRE2010_LEGACY + PRE2010_NEW

DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")

# ------------------------------------------------------------------------------------------------
# Dissolved states coded with successor codes (QIDs from registry/entities/historical_states_qids.json)
# ------------------------------------------------------------------------------------------------

_YUGOSLAVIA = (date(1992, 4, 27), "Q36704", "Yugoslavia (1918-1992)")
_FRY = (date(2003, 2, 4), "Q838261", "Federal Republic of Yugoslavia")

#: provider code -> ordered windows (exclusive end date, QID, label): a date before ``until`` denotes
#: that historical state. After the last window the code is the crosswalk country (unless dissolved).
HISTORICAL_WINDOWS: dict[str, tuple[tuple[date, str, str], ...]] = {
    "RUS": ((date(1991, 12, 26), "Q15180", "Soviet Union"),),
    "CZE": ((date(1993, 1, 1), "Q33946", "Czechoslovakia"),),
    "SVK": ((date(1993, 1, 1), "Q33946", "Czechoslovakia"),),
    "SRB": (_YUGOSLAVIA, _FRY, (date(2006, 6, 5), "Q37024", "Serbia and Montenegro")),
    "MNE": (_YUGOSLAVIA, _FRY, (date(2006, 6, 3), "Q37024", "Serbia and Montenegro")),
    "YUG": (_YUGOSLAVIA, _FRY),
    "SCG": (_FRY, (date(2006, 6, 5), "Q37024", "Serbia and Montenegro")),
}
#: codes of dissolved states: never a crosswalk country, whatever the date
DISSOLVED_CODES = frozenset({"YUG", "SCG"})


def _window(code: str, d: date) -> tuple[str, str] | None:
    for until, qid, label in HISTORICAL_WINDOWS.get(code, ()):
        if d < until:
            return qid, label
    return None


@lru_cache(maxsize=65536)
def resolve_geo(code: str, start: date, end: date) -> tuple[str | None, str | None, dict[str, Any]]:
    """Provider ISO3-like code + half-open period -> (crosswalk iso3, historical QID, flags).

    Cached: callers must not mutate the returned flags (they are spread into new dicts)."""
    from reva.atlas.tables import country_iso3

    c = (code or "").strip().upper()
    if not c:
        return None, None, {}
    last = end - timedelta(days=1)
    a, b = _window(c, start), _window(c, last)
    if a != b:
        return (
            None,
            None,
            {
                "geo_note": f"provider code {c} for a period during which "
                f"{(a or ('', 'the successor state'))[1]} was replaced by {(b or ('', 'the successor state'))[1]}: "
                "no country or historical state assigned (not guessed)",
                "state_transition_in_period": True,
            },
        )
    if a is not None:
        return (
            None,
            a[0],
            {
                "geo_note": f"provider code {c} used for a date when the state was {a[1]} ({a[0]}); kept unmapped "
                "(historical state)",
                "historical_state": a[1],
            },
        )
    if c in DISSOLVED_CODES:
        return None, None, {"geo_note": f"provider code {c} of a dissolved state used after its dissolution"}
    return country_iso3(c, GEO_SCHEME), None, {}


# ------------------------------------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------------------------------------


def day_period(d: date) -> tuple[date, date]:
    return d, d + timedelta(days=1)


def year_bounds(y: int) -> tuple[date, date]:
    return date(y, 1, 1), date(y + 1, 1, 1)


def read_csv(body: bytes, key: str, *, strict: bool) -> list[dict[str, str]]:
    """Decode (UTF-8, strict) and parse one Hub CSV; refuse schema drift and (live) truncated files."""
    hub_id, _, min_rows, required = FILES[key]
    try:
        text_ = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValidationProblem("Hub CSV is not UTF-8", file=hub_id, error=str(e)[:200]) from e
    rd = csv.DictReader(io.StringIO(text_, newline=""))
    cols = list(rd.fieldnames or [])
    missing = [c for c in required if c not in cols]
    if missing:
        raise ValidationProblem("Hub CSV schema changed (missing columns): not ingested", file=hub_id, missing=missing)
    rows = [r for r in rd]
    if strict and len(rows) < min_rows:
        raise ValidationProblem("Hub CSV has fewer rows than expected", file=hub_id, rows=len(rows), minimum=min_rows)
    return rows


def check_hdx_record(body: bytes, key: str) -> dict[str, Any]:
    """The publisher's HDX record of a dataset: licence and organisation must be the expected ones."""
    _, name, _, _ = FILES[key]
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise ValidationProblem("HDX record is not JSON", dataset=name) from e
    r = doc.get("result") if isinstance(doc, Mapping) and doc.get("success") else None
    if not isinstance(r, Mapping) or r.get("name") != name:
        raise ValidationProblem("HDX record missing or for another dataset", dataset=name)
    lic, org = r.get("license_id"), (r.get("organization") or {}).get("name")
    if lic != EXPECTED_LICENCE_ID or org != EXPECTED_ORGANISATION:
        raise ValidationProblem(
            "HDX licence or publisher changed: dataset not ingested (licence review needed)",
            dataset=name,
            licence_id=str(lic)[:64],
            organisation=str(org)[:64],
        )
    return {
        "hdx_dataset": name,
        "licence_id": lic,
        "licence_title": r.get("license_title"),
        "licence_url": r.get("license_url"),
        "hdx_last_modified": r.get("last_modified"),
        "hdx_resources": [
            {"name": x.get("name"), "last_modified": x.get("last_modified"), "size": x.get("size")}
            for x in r.get("resources") or []
        ],
    }


def _num(v: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """(strict decimal, None) | (None, None) for an empty cell | (None, missing flags) for a non-decimal."""
    s = (v or "").strip()
    if not s:
        return None, None
    if DECIMAL.match(s):
        return s, None
    return None, {"missing": "unreliable", "provider_value": s[:100]}


def _fmt_dec(total: Any) -> str:
    """A Decimal as a strict decimal string (``2603.0`` -> ``2603``, ``32.170`` -> ``32.17``)."""
    return format(total.normalize(), "f") if total != total.to_integral() else str(int(total))


def _dec_sum(values: Iterable[str]) -> str:
    from decimal import Decimal

    return _fmt_dec(sum((Decimal(v) for v in values), Decimal(0)))


def _iso_date(s: str | None) -> date | None:
    t = (s or "").strip()
    if not t:
        return None
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        return None


def _fl(**kw: Any) -> str:
    return json.dumps({k: v for k, v in kw.items() if v is not None}, sort_keys=True, ensure_ascii=False)


def _sel(**kw: Any) -> str:
    return json.dumps(kw, sort_keys=True, ensure_ascii=False)


def mission_slug(acronym: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", acronym.strip()).strip("_")
    return s[:110]


def _row(**kw: Any) -> dict[str, Any]:
    kw.setdefault("source", ATLAS_SOURCE)
    kw.setdefault("concept_id", None)
    kw.setdefault("value_text", None)
    return kw


# ------------------------------------------------------------------------------------------------
# Atlas tables
# ------------------------------------------------------------------------------------------------


class _Totals:
    """Accumulates computed totals per (indicator, provider_geo, period) with their inputs."""

    def __init__(self) -> None:
        self.acc: dict[tuple[str, str, date, date], dict[str, Any]] = {}

    def add(
        self, indicator: str, geo: str, start: date, end: date, value: str, *, raw: str, file: str, flt: dict[str, Any]
    ) -> None:
        k = (indicator, geo, start, end)
        e = self.acc.setdefault(k, {"values": [], "raw": raw, "file": file, "filter": flt})
        e["values"].append(value)


def monthly_rows(rows: Sequence[Mapping[str, str]], *, raw_sha256: str, totals: _Totals | None = None) -> AtlasRows:
    out = AtlasRows()
    file = FILES["monthly"][0] + ".csv"
    for i, r in enumerate(rows, start=1):
        d = _iso_date(r["last_reporting_date"])
        code = (r["isocode3"] or "").strip().upper()
        mission = (r["mission_acronym"] or "").strip()
        ptype = (r["personnel_type"] or "").strip()
        if d is None or not code or not mission or not ptype:
            continue
        start, end = day_period(d)
        iso3, qid, gflags = resolve_geo(code, start, end)
        for sex, col in (("male", "male_personnel"), ("female", "female_personnel")):
            v, bad = _num(r[col])
            if v is None and bad is None:
                continue
            out.add(
                _row(
                    dataset="contrib_monthly",
                    indicator=f"uniformed_personnel|mission={mission}|type={ptype}|sex={sex}",
                    iso3=iso3,
                    provider_geo=code,
                    entity_qid=qid,
                    period_start=start,
                    period_end=end,
                    value=v,
                    unit="persons",
                    flags=_fl(
                        as_of="last reporting date of the month (deployment strength at that date)",
                        contributing_country=r["contributing_country"],
                        **gflags,
                        **(bad or {}),
                    ),
                    raw_sha256=raw_sha256,
                    selector=_sel(file=file, row=i, column=col),
                )
            )
            if totals is not None and v is not None:
                flt = {"isocode3": code, "last_reporting_date": d.isoformat()}
                totals.add("uniformed_personnel_total", code, start, end, v, raw=raw_sha256, file=file, flt=flt)
                if sex == "female":
                    totals.add(
                        "uniformed_personnel_total|sex=female", code, start, end, v, raw=raw_sha256, file=file, flt=flt
                    )
    return out


def pre2010_rows(
    rows: Sequence[Mapping[str, str]], *, raw_sha256: str, totals: _Totals | None = None
) -> tuple[AtlasRows, dict[str, int]]:
    out = AtlasRows()
    file = FILES["pre2010"][0] + ".csv"
    checks = {"overlap_rows": 0, "overlap_consistent": 0, "overlap_inconsistent": 0}
    for i, r in enumerate(rows, start=1):
        d = _iso_date(r["Date"])
        code = (r["Contributor_ISO-3"] or "").strip().upper()
        mission = (r["Mission"] or "").strip()
        if d is None or not code or not mission:
            continue
        start, end = day_period(d)
        iso3, qid, gflags = resolve_geo(code, start, end)
        vals: dict[str, str] = {}
        for col in PRE2010_CATEGORIES:
            v, bad = _num(r[col])
            if v is None and bad is None:
                continue
            if v is not None:
                vals[col] = v
            overlap = None
            if col in PRE2010_NEW:
                overlap = (
                    "late-2009 category; overlaps the legacy categories (Civilian_Police = Individual_Police + "
                    "Formed_Police_Units, Observers = Experts_on_Mission): do not add to them"
                )
            out.add(
                _row(
                    dataset="contrib_pre2010",
                    indicator=f"uniformed_personnel|mission={mission}|type={col}",
                    iso3=iso3,
                    provider_geo=code,
                    entity_qid=qid,
                    period_start=start,
                    period_end=end,
                    value=v,
                    unit="persons",
                    flags=_fl(
                        as_of="month-end reporting date",
                        contributor=r["Contributor"],
                        mission_country_as_reported=(r["Mission_Country"] or None),
                        category_overlap=overlap,
                        **gflags,
                        **(bad or {}),
                    ),
                    raw_sha256=raw_sha256,
                    selector=_sel(file=file, row=i, column=col),
                )
            )
        if any(c in vals for c in PRE2010_NEW):
            from decimal import Decimal

            checks["overlap_rows"] += 1
            police_new = sum(Decimal(vals[c]) for c in ("Individual_Police", "Formed_Police_Units") if c in vals)
            obs_new = Decimal(vals.get("Experts_on_Mission", "0"))
            ok = police_new == Decimal(vals.get("Civilian_Police", "0")) and obs_new == Decimal(
                vals.get("Observers", "0")
            )
            checks["overlap_consistent" if ok else "overlap_inconsistent"] += 1
        if totals is not None:
            for col in PRE2010_LEGACY:
                if col in vals:
                    totals.add(
                        "uniformed_personnel_total",
                        code,
                        start,
                        end,
                        vals[col],
                        raw=raw_sha256,
                        file=file,
                        flt={"Contributor_ISO-3": code, "Date": d.isoformat(), "columns": list(PRE2010_LEGACY)},
                    )
    return out, checks


def annual_rows(
    rows: Sequence[Mapping[str, str]], *, raw_sha256: str, current_year: int, totals: _Totals | None = None
) -> AtlasRows:
    out = AtlasRows()
    file = FILES["annual"][0] + ".csv"
    for i, r in enumerate(rows, start=1):
        y = (r["Year"] or "").strip()
        code = (r["ISOCode3"] or "").strip().upper()
        ptype_raw = r["Personnel_Type"] or ""
        ptype = ptype_raw.strip()
        if not y.isdigit() or not code or not ptype:
            continue
        start, end = year_bounds(int(y))
        iso3, qid, gflags = resolve_geo(code, start, end)
        incomplete = True if int(y) >= current_year else None
        for sex, col in (("male", "Male"), ("female", "Female")):
            v, bad = _num(r[col])
            if v is None and bad is None:
                continue
            out.add(
                _row(
                    dataset="contrib_annual",
                    indicator=f"annual_average_uniformed_personnel|type={ptype}|sex={sex}",
                    iso3=iso3,
                    provider_geo=code,
                    entity_qid=qid,
                    period_start=start,
                    period_end=end,
                    value=v,
                    unit="persons (annual average)",
                    flags=_fl(
                        provider_note="annual average of monthly figures; not unique individuals, not 31 December",
                        personnel_type_as_published=ptype_raw if ptype_raw != ptype else None,
                        year_incomplete_at_retrieval=incomplete,
                        **gflags,
                        **(bad or {}),
                    ),
                    raw_sha256=raw_sha256,
                    selector=_sel(file=file, row=i, column=col),
                )
            )
            if totals is not None and v is not None:
                flt = {"ISOCode3": code, "Year": y}
                for ind in (
                    "annual_average_uniformed_personnel_total",
                    f"annual_average_uniformed_personnel_total|type={ptype}",
                    *(("annual_average_uniformed_personnel_total|sex=female",) if sex == "female" else ()),
                ):
                    totals.add(ind, code, start, end, v, raw=raw_sha256, file=file, flt=flt)
    return out


def fatality_rows(
    rows: Sequence[Mapping[str, str]], *, raw_sha256: str, current_year: int, totals: _Totals | None = None
) -> tuple[AtlasRows, dict[str, int]]:
    """One cell per (nationality, mission acronym, type, year). Provider rows repeated under several
    mission ids with identical values (UNEF under ids 44/45/46) are collapsed and flagged; differing
    repeats get no value (``missing: unreliable``) and list the provider values."""
    out = AtlasRows()
    file = FILES["fatalities"][0] + ".csv"
    groups: dict[tuple[str, str, str, str], list[tuple[int, Mapping[str, str]]]] = defaultdict(list)
    for i, r in enumerate(rows, start=1):
        y = (r["Incident_Year"] or "").strip()
        if not y.isdigit():
            continue
        code = (r["ISOCode3"] or "").strip().upper()
        nat = (r["Casualty_Nationality"] or "").strip()
        geo = code or nat or "(blank)"
        groups[(geo, (r["Mission"] or "").strip() or "(blank)", (r["Type_of_Fatalities"] or "").strip(), y)].append(
            (i, r)
        )
    stats = {"collapsed_identical_repeats": 0, "conflicting_repeats": 0}
    for (geo, mission, ftype, y), items in sorted(groups.items()):
        start, end = year_bounds(int(y))
        code = (items[0][1]["ISOCode3"] or "").strip().upper()
        iso3, qid, gflags = resolve_geo(code, start, end) if code else (None, None, {})
        values = [(i, _num(r["Fatalities"])) for i, r in items]
        distinct = {v for _, (v, _) in values}
        extra: dict[str, Any] = {}
        value: str | None
        if len(items) > 1:
            ids = sorted({(r["mission_id"] or "").strip() for _, r in items})
            if len(distinct) == 1:
                stats["collapsed_identical_repeats"] += 1
                extra["provider_repeated_rows"] = {"rows": [i for i, _ in items], "mission_ids": ids}
            else:
                stats["conflicting_repeats"] += 1
                extra.update(missing="unreliable", provider_values=[v for _, (v, _) in values], mission_ids=ids)
        value, bad = values[0][1] if len(distinct) == 1 else (None, None)
        if value is None and not extra.get("missing") and bad is None:
            continue
        nat = (items[0][1]["Casualty_Nationality"] or "").strip()
        out.add(
            _row(
                dataset="fatalities",
                indicator=f"fatalities|mission={mission}|type={ftype or '(blank)'}",
                iso3=iso3,
                provider_geo=geo,
                entity_qid=qid,
                period_start=start,
                period_end=end,
                value=value,
                unit="deaths",
                flags=_fl(
                    casualty_nationality=nat or None,
                    mission_id=(items[0][1]["mission_id"] or "").strip() or None,
                    provider_note="nationality of the casualties, not the incident location; data before 2000 may be "
                    "incomplete (provider)",
                    provider_geo_blank=True if geo == "(blank)" else None,
                    year_incomplete_at_retrieval=True if int(y) >= current_year else None,
                    **gflags,
                    **(bad or {}),
                    **extra,
                ),
                raw_sha256=raw_sha256,
                selector=_sel(file=file, rows=[i for i, _ in items], column="Fatalities"),
            )
        )
        if totals is not None and value is not None:
            flt = {"ISOCode3/Casualty_Nationality": geo, "Incident_Year": y}
            totals.add("fatalities_total", geo, start, end, value, raw=raw_sha256, file=file, flt=flt)
            totals.add(f"fatalities_total|type={ftype}", geo, start, end, value, raw=raw_sha256, file=file, flt=flt)
    return out, stats


_TOTAL_METHODS = {
    "uniformed_personnel_total": "sum of every reported cell for the country and date over missions and personnel "
    "types: 2010+ male_personnel + female_personnel (DPO-UCHISTORICAL); 1990-2009 Troops + Civilian_Police + "
    "Observers (DPPADPO-UCPre2010; the late-2009 categories overlap these and are not added)",
    "uniformed_personnel_total|sex=female": "sum of female_personnel over missions and personnel types (2010+)",
    "annual_average_uniformed_personnel_total": "sum of the provider's annual averages over personnel types and sexes",
    "annual_average_uniformed_personnel_total|sex=female": "sum of the provider's female annual averages over types",
    "fatalities_total": "sum of fatalities over missions and types of fatality (repeated provider rows counted once)",
}


def totals_rows(totals: _Totals, *, current_year: int) -> AtlasRows:
    out = AtlasRows()
    for (ind, geo, start, end), e in sorted(totals.acc.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        iso3, qid, gflags = resolve_geo(geo, start, end) if re.fullmatch(r"[A-Z]{3}", geo) else (None, None, {})
        base = ind.split("|", 1)[0]
        method = _TOTAL_METHODS.get(ind) or _TOTAL_METHODS.get(base, "sum") + (
            f" ({ind.split('|', 1)[1]} only)" if "|" in ind else ""
        )
        unit = (
            "deaths"
            if base == "fatalities_total"
            else ("persons (annual average)" if base.startswith("annual") else "persons")
        )
        yearly = end - start > timedelta(days=1)
        out.add(
            _row(
                dataset="totals",
                indicator=ind,
                iso3=iso3,
                provider_geo=geo,
                entity_qid=qid,
                period_start=start,
                period_end=end,
                value=_dec_sum(e["values"]),
                unit=unit,
                flags=_fl(
                    computed_from=f"{ATLAS_SOURCE} {e['file']}",
                    method=method,
                    cells_summed=len(e["values"]),
                    year_incomplete_at_retrieval=True if yearly and start.year >= current_year else None,
                    **gflags,
                ),
                raw_sha256=e["raw"],
                selector=_sel(file=e["file"], filter=e["filter"]),
            )
        )
    return out


# ------------------------------------------------------------------------------------------------
# KG records
# ------------------------------------------------------------------------------------------------

#: dataset predicates: pid -> (value kind, reva predicate, en, fr, inverse name, inverse en)
UNPK_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "unpk:contributed_personnel_to": (
        "item",
        "un_peacekeeping_contribution_to",
        "contributed uniformed personnel to (UN peace operation)",
        "a fourni du personnel en uniforme à (opération de paix de l'ONU)",
        "received_un_uniformed_personnel_from",
        "received uniformed personnel from",
    ),
    "unpk:mission_country": (
        "item",
        "un_mission_country",
        "mission country as reported by the UN Peace and Security Data Hub",
        "pays de la mission (selon le Peace and Security Data Hub de l'ONU)",
        "country_of_un_mission",
        "country of UN mission (as reported)",
    ),
    "unpk:start_date": ("time", "un_mission_start", "start date (UN peace operation)", "date de début", None, None),
    "unpk:end_date": (
        "time",
        "un_mission_end",
        "end / mandate expiration date (UN peace operation)",
        "date de fin / d'expiration du mandat",
        None,
        None,
    ),
    "unpk:hq_location": (
        "string",
        "un_mission_hq_location",
        "headquarters location (UN peace operation, as published)",
        "siège de la mission (tel que publié)",
        None,
        None,
    ),
    "unpk:initial_mandate": (
        "string",
        "un_mission_initial_mandate",
        "initial mandate document symbol",
        "cote du mandat initial",
        None,
        None,
    ),
    "unpk:lead_entity": (
        "string",
        "un_mission_lead_entity",
        "mission type / lead entity (Field Missions Dashboard)",
        "type de mission / entité responsable",
        None,
        None,
    ),
}


def kg_properties() -> list[Any]:
    from reva.kg.model import KgProperty

    return [
        KgProperty(
            pid=p,
            value_kind=k,
            reva_predicate=rp,
            label_en=le,
            label_fr=lf,
            inverse_name=inv,
            inverse_label_en=ile,
        )
        for p, (k, rp, le, lf, inv, ile) in UNPK_PROPERTIES.items()
    ]


class _Countries:
    """Country / historical-state KG node for a provider code at a date (QIDs when known)."""

    def __init__(self, qid_for_iso3: Mapping[str, str]) -> None:
        self.qid_for_iso3 = dict(qid_for_iso3)
        self.extra: dict[str, str] = {}

    def node(self, code: str, start: date, end: date) -> tuple[str | None, dict[str, Any]]:
        iso3, qid, flags = resolve_geo(code, start, end)
        if qid:
            return qid, flags
        if iso3:
            q = self.qid_for_iso3.get(iso3)
            if q:
                return q, flags
            eid = f"iso3:{iso3}"
            self.extra.setdefault(eid, iso3)
            return eid, flags
        return None, flags

    def entities(self) -> list[KgEntity]:
        return [
            KgEntity(
                qid=eid,
                kind="country_code",
                labels={"en": f"{iso3} (ISO 3166-1 alpha-3)"},
                descriptions={"en": "crosswalk country without a Wikidata QID in registry/entities/country_qids.json"},
            )
            for eid, iso3 in sorted(self.extra.items())
        ]


def _mission_entity(slug: str, label: str | None, acronym: str, origin: str) -> KgEntity:
    lab = label or acronym
    return KgEntity(
        qid=f"unpk:mission:{slug}",
        kind="un_peace_operation",
        labels={"en": lab if lab == acronym else f"{lab} ({acronym})"},
        descriptions={"en": f"UN peace operation or field mission, acronym '{acronym}' as used in {origin}"},
    )


def kg_records(
    parsed: Mapping[str, Sequence[Mapping[str, str]]],
    *,
    shas: Mapping[str, str],
    qid_for_iso3: Mapping[str, str],
) -> tuple[list[KgEntity], list[KgStatement], dict[str, Any]]:
    from reva.atlas.tables import country_iso3

    countries = _Countries(qid_for_iso3)
    entities: dict[str, KgEntity] = {}
    stmts: list[KgStatement] = []
    report: dict[str, Any] = {"skipped": defaultdict(int), "mission_country_unmapped": []}
    names: dict[str, str] = {}
    for r in parsed.get("pko", ()):
        acr = (r["mission_acronym"] or "").strip()
        if acr and acr != "*" and (r["mission_name"] or "").strip():
            names.setdefault(acr, r["mission_name"].strip())
    for r in parsed.get("fieldmissions", ()):
        acr = (r["missions"] or "").strip()
        if acr and (r["mission_name"] or "").strip():
            names.setdefault(acr, r["mission_name"].strip())

    def mission(acr: str, origin: str) -> str:
        slug = mission_slug(acr)
        eid = f"unpk:mission:{slug}"
        if eid not in entities:
            entities[eid] = _mission_entity(slug, names.get(acr), acr, origin)
        return eid

    # --- missions: PKO list (dates, HQ) ---
    pko_file = FILES["pko"][0] + ".csv"
    for i, r in enumerate(parsed.get("pko", ()), start=1):
        acr = (r["mission_acronym"] or "").strip()
        if not acr or acr == "*":
            report["skipped"]["pko_unspecified_operation"] += 1
            continue
        m = mission(acr, "the DPPADPOSS-PKO list")
        mid = (r["mission_id"] or "").strip()
        base = {"provider_mission_id": mid, "provider_last_update": (r["last_update"] or "").strip() or None}
        for col, pid in (("start_date", "unpk:start_date"), ("end_date", "unpk:end_date")):
            d = _iso_date(r[col])
            if d is None:
                continue
            q = dict(base)
            if col == "end_date":
                q["provider_definition"] = "Mandate expiration date of the Mission as defined by the Security Council"
            stmts.append(
                KgStatement(
                    key=f"pko:{mid or acr}:{col}",
                    subject=m,
                    property=pid,
                    value={"type": "time", "time": d.isoformat(), "precision": 11},
                    qualifiers={k: v for k, v in q.items() if v is not None},
                    raw_sha256=shas["pko"],
                    selector={"file": pko_file, "row": i, "column": col},
                )
            )
        loc = (r["mission_location"] or "").strip()
        if loc:
            q = dict(base)
            lon, lat = (r["mission_longitude"] or "").strip(), (r["mission_latitude"] or "").strip()
            if lon or lat:
                q.update(
                    longitude_as_published=lon,
                    latitude_as_published=lat,
                    coordinates_note="published with a decimal comma; some rows appear to have latitude and "
                    "longitude swapped; kept verbatim, not corrected",
                )
            stmts.append(
                KgStatement(
                    key=f"pko:{mid or acr}:hq",
                    subject=m,
                    property="unpk:hq_location",
                    value={"type": "string", "value": loc},
                    qualifiers={k: v for k, v in q.items() if v is not None},
                    raw_sha256=shas["pko"],
                    selector={"file": pko_file, "row": i, "column": "mission_location"},
                )
            )

    # --- missions: Field Missions Dashboard (location, mandate, lead entity) ---
    fm_file = FILES["fieldmissions"][0] + ".csv"
    for i, r in enumerate(parsed.get("fieldmissions", ()), start=1):
        acr = (r["missions"] or "").strip()
        sym = (r["initial_symbol"] or "").strip()
        if not acr or not sym:
            report["skipped"]["fieldmissions_note_or_blank_row"] += 1
            continue
        m = mission(acr, "the Field Missions Dashboard")
        q = {"provider_last_update": (r["last_update"] or "").strip() or None}
        q = {k: v for k, v in q.items() if v is not None}
        stmts.append(
            KgStatement(
                key=f"fm:{mission_slug(acr)}:initial_mandate",
                subject=m,
                property="unpk:initial_mandate",
                value={"type": "string", "value": sym},
                qualifiers=q,
                raw_sha256=shas["fieldmissions"],
                selector={"file": fm_file, "row": i, "column": "initial_symbol"},
            )
        )
        lead = (r["lead_entity"] or "").strip()
        if lead:
            stmts.append(
                KgStatement(
                    key=f"fm:{mission_slug(acr)}:lead_entity",
                    subject=m,
                    property="unpk:lead_entity",
                    value={"type": "string", "value": lead},
                    qualifiers=q,
                    raw_sha256=shas["fieldmissions"],
                    selector={"file": fm_file, "row": i, "column": "lead_entity"},
                )
            )
        loc = (r["location"] or "").strip()
        iso3 = country_iso3(loc, LOCATION_SCHEME) if loc else None
        if iso3 is None:
            if loc:
                report["mission_country_unmapped"].append({"mission": acr, "location": loc})
            continue
        node = qid_for_iso3.get(iso3)
        if node is None:
            node = f"iso3:{iso3}"
            countries.extra.setdefault(node, iso3)
        stmts.append(
            KgStatement(
                key=f"fm:{mission_slug(acr)}:country:{iso3}",
                subject=m,
                property="unpk:mission_country",
                object_qid=node,
                qualifiers={
                    **q,
                    "provider_field": "location (Field Missions Dashboard)",
                    "provider_value": loc,
                    "mapping": f"geo alias scheme {LOCATION_SCHEME} (registry/atlas/geo_aliases/a_military.yaml)",
                    "note": "where the mission operates as reported by the UN; not a statement about sovereignty",
                },
                raw_sha256=shas["fieldmissions"],
                selector={"file": fm_file, "row": i, "column": "location"},
            )
        )

    # --- contributions (country -> mission per calendar year) and pre-2010 mission countries ---
    contrib: dict[tuple[str, str, int], dict[str, Any]] = {}
    mcountry: dict[tuple[str, str], dict[str, Any]] = {}

    def add_contrib(
        node: str, acr: str, d: date, total: Any, types: Iterable[str], file: str, raw: str, origin: str
    ) -> None:
        m = mission(acr, origin)
        e = contrib.setdefault(
            (node, m, d.year), {"dates": {}, "types": set(), "file": file, "raw": raw, "acr": acr}
        )
        e["dates"][d] = e["dates"].get(d, 0) + total
        e["types"].update(types)

    from decimal import Decimal

    per_date: dict[tuple[str, str, date], dict[str, Any]] = {}
    for r in parsed.get("monthly", ()):
        d = _iso_date(r["last_reporting_date"])
        code = (r["isocode3"] or "").strip().upper()
        acr = (r["mission_acronym"] or "").strip()
        if d is None or not code or not acr:
            continue
        tot = Decimal(0)
        for col in ("male_personnel", "female_personnel"):
            v, _ = _num(r[col])
            if v is not None:
                tot += Decimal(v)
        e = per_date.setdefault((code, acr, d), {"total": Decimal(0), "types": set()})
        e["total"] += tot
        if tot > 0:
            e["types"].add((r["personnel_type"] or "").strip())
    for (code, acr, d), e in per_date.items():
        if e["total"] <= 0:
            report["skipped"]["contribution_reported_zero"] += 1
            continue
        node, _ = countries.node(code, *day_period(d))
        if node is None:
            report["skipped"]["contribution_country_unresolved"] += 1
            continue
        add_contrib(node, acr, d, e["total"], e["types"], FILES["monthly"][0], shas["monthly"], "DPO-UCHISTORICAL")
    for i, r in enumerate(parsed.get("pre2010", ()), start=1):
        d = _iso_date(r["Date"])
        code = (r["Contributor_ISO-3"] or "").strip().upper()
        acr = (r["Mission"] or "").strip()
        if d is None or not acr:
            continue
        mc = (r["Mission_Country_ISO-3"] or "").strip().upper()
        if mc:
            mnode, _ = countries.node(mc, *day_period(d))
            if mnode is not None:
                k = (mission(acr, "DPPADPO-UCPre2010"), mnode)
                e2 = mcountry.setdefault(
                    k, {"first": d, "last": d, "row": i, "value": r["Mission_Country"], "code": mc, "n": 0}
                )
                e2["first"], e2["last"], e2["n"] = min(e2["first"], d), max(e2["last"], d), e2["n"] + 1
        if not code:
            continue
        tot = Decimal(0)
        types = []
        for col in PRE2010_LEGACY:
            v, _ = _num(r[col])
            if v is not None and Decimal(v) > 0:
                tot += Decimal(v)
                types.append(col)
        if tot <= 0:
            report["skipped"]["contribution_reported_zero"] += 1
            continue
        node, _ = countries.node(code, *day_period(d))
        if node is None:
            report["skipped"]["contribution_country_unresolved"] += 1
            continue
        add_contrib(node, acr, d, tot, types, FILES["pre2010"][0], shas["pre2010"], "DPPADPO-UCPre2010")

    for (node, m, year), e in sorted(contrib.items()):
        dates = sorted(e["dates"])
        mx = max(e["dates"].values())
        stmts.append(
            KgStatement(
                key=f"contrib:{node}:{m.rsplit(':', 1)[1]}:{year}",
                subject=node,
                property="unpk:contributed_personnel_to",
                object_qid=m,
                start=dates[0].isoformat(),
                start_precision=11,
                end=dates[-1].isoformat(),
                end_precision=11,
                qualifiers={
                    "reporting_dates": len(dates),
                    "max_uniformed_personnel_reported": _fmt_dec(mx),
                    "max_computed": "REVA sum over personnel types (and sexes) at one reporting date",
                    "personnel_types": sorted(e["types"]),
                    "dataset": e["file"],
                    "year": year,
                },
                raw_sha256=e["raw"],
                selector={"file": e["file"] + ".csv", "filter": {"mission": e["acr"], "year": year}},
            )
        )
    for (m, node), e in sorted(mcountry.items()):
        stmts.append(
            KgStatement(
                key=f"pre2010:{m.rsplit(':', 1)[1]}:country:{node}",
                subject=m,
                property="unpk:mission_country",
                object_qid=node,
                start=e["first"].isoformat(),
                start_precision=11,
                end=e["last"].isoformat(),
                end_precision=11,
                qualifiers={
                    "provider_field": "Mission_Country / Mission_Country_ISO-3 (DPPADPO-UCPre2010)",
                    "provider_value": e["value"],
                    "provider_code": e["code"],
                    "reporting_dates_observed": e["n"],
                    "note": "dates are the first and last reporting dates of the dataset with this coding, not the "
                    "mission's mandate; where the mission operates as reported, not a statement about sovereignty",
                },
                raw_sha256=shas["pre2010"],
                selector={"file": FILES["pre2010"][0] + ".csv", "row": e["row"], "column": "Mission_Country_ISO-3"},
            )
        )
    ents = sorted(entities.values(), key=lambda e: e.qid) + countries.entities()
    report["skipped"] = dict(report["skipped"])
    report["contribution_statements"] = len(contrib)
    report["mission_country_statements"] = len(mcountry) + sum(
        1 for s in stmts if s.property == "unpk:mission_country" and s.key.startswith("fm:")
    )
    return ents, stmts, report


# ------------------------------------------------------------------------------------------------
# Platform entry points
# ------------------------------------------------------------------------------------------------


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="United Nations Peace and Security Data Hub (DPPA-DPO Information Management Unit)",
        licence=RIGHTS["licence"] + " (per dataset, publisher's HDX records; checked every run)",
        allowed_hosts=(API_HOST, HDX_HOST),
        datasets=DATASETS,
        dataset_ids=tuple(f"psdata:{v[0]}" for v in FILES.values()),
        cadence_seconds=30 * 86400,
        planned_requests=2 * len(FILES) + 2,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "geo": "ISO 3166 alpha-3 (scheme unpk); dissolved states re-coded by the provider kept unmapped with QIDs",
            "time": "month-end reporting date (day period) / calendar year",
            "kg": "unpk:mission:<acronym>; country -> mission contributions per year",
        },
        notes="Data from the Hub API (api.psdata.un.org); licence from the publisher's HDX records "
        "(data.humdata.org package_show), CC BY 3.0 IGO.",
    )


def files_for(dataset: str | None) -> list[str]:
    if dataset is None:
        return list(FILES)
    return sorted(set(DATASET_FILES[dataset]))


def build(
    raws: Mapping[str, Any],
    *,
    current_year: int,
    strict: bool,
    want: Iterable[str] = DATASETS,
    with_kg: bool = True,
    qid_for_iso3: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Parse the CSV raws (``raws[key].body``) into atlas row sets and KG records (no I/O)."""
    want = set(want)
    shas = {k: hashlib.sha256(r.body).hexdigest() for k, r in raws.items()}
    parsed = {k: read_csv(r.body, k, strict=strict) for k, r in raws.items()}
    totals = _Totals() if "totals" in want else None
    out: dict[str, Any] = {"shas": shas, "row_counts": {k: len(v) for k, v in parsed.items()}, "atlas": {}}
    if "monthly" in parsed and ({"contrib_monthly", "totals"} & want):
        out["atlas"]["contrib_monthly"] = monthly_rows(parsed["monthly"], raw_sha256=shas["monthly"], totals=totals)
    if "pre2010" in parsed and ({"contrib_pre2010", "totals"} & want):
        rows, checks = pre2010_rows(parsed["pre2010"], raw_sha256=shas["pre2010"], totals=totals)
        out["atlas"]["contrib_pre2010"], out["pre2010_overlap_check"] = rows, checks
    if "annual" in parsed and ({"contrib_annual", "totals"} & want):
        out["atlas"]["contrib_annual"] = annual_rows(
            parsed["annual"], raw_sha256=shas["annual"], current_year=current_year, totals=totals
        )
    if "fatalities" in parsed and ({"fatalities", "totals"} & want):
        rows, stats = fatality_rows(
            parsed["fatalities"], raw_sha256=shas["fatalities"], current_year=current_year, totals=totals
        )
        out["atlas"]["fatalities"], out["fatalities_repeats"] = rows, stats
    if totals is not None:
        out["atlas"]["totals"] = totals_rows(totals, current_year=current_year)
    if with_kg and all(k in parsed for k in KG_FILES):
        ents, stmts, rep = kg_records(parsed, shas=shas, qid_for_iso3=qid_for_iso3 or {})
        out.update(entities=ents, statements=stmts, kg_report=rep)
    return out


_COVERAGE = {
    "contrib_monthly": "UN Peace and Security Data Hub DPO-UCHISTORICAL: uniformed personnel by contributing country, "
    "mission, personnel type and sex at each month-end reporting date, 2010-01 -> latest; values verbatim",
    "contrib_pre2010": "DPPADPO-UCPre2010 (archived): uniformed personnel by contributor and mission, 1990-11 -> "
    "2009-12; late-2009 categories overlap the legacy ones (flagged); dissolved states kept unmapped with QIDs",
    "contrib_annual": "DataCommons-UCHISTORICAL: provider annual averages by country, personnel type and sex, "
    "2010 -> current (incomplete) year",
    "fatalities": "DataCommons-Fatalities: fatalities by casualty nationality, mission, type and year, 1948 -> latest; "
    "repeated provider rows collapsed and flagged",
    "totals": "COMPUTED by REVA (flags.computed_from): per-country totals of uniformed personnel per reporting date "
    "and per year (annual averages) and fatalities per year",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Check the HDX licence records, fetch the Hub CSVs, archive them after parsing, register the atlas
    tables and (full runs, ``kg`` schema present) load the KG records."""
    from datetime import UTC, datetime

    keys = files_for(run.dataset)
    with_kg = run.dataset is None and str(run.options.get("kg", "true")).lower() != "false"
    if with_kg:
        keys = list(FILES)
    licences: dict[str, Any] = {}
    hdx_raws: dict[str, Any] = {}
    for k in keys:
        raw = await run.get(
            HDX_URL.format(name=FILES[k][1]), key=f"hdx:{FILES[k][1]}", media_types=("application/json",), max_bytes=2_000_000
        )
        licences[k] = check_hdx_record(raw.body, k)
        hdx_raws[k] = raw
    raws: dict[str, Any] = {}
    for k in keys:
        raws[k] = await run.get(API_URL.format(id=FILES[k][0]), key=f"psdata:{FILES[k][0]}", media_types=("text/csv",))
    want = set(DATASETS) if run.dataset is None else {run.dataset}
    retrieved = max(r.retrieved_at for r in raws.values())
    out = build(
        raws,
        current_year=(retrieved or datetime.now(UTC)).year,
        strict=run.origin == "live",
        want=want,
        with_kg=with_kg,
        qid_for_iso3=load_qid_for_iso3() if with_kg else None,
    )
    hdx_shas = {k: run.archive(r) for k, r in hdx_raws.items()}  # parsed and checked first
    for r in raws.values():
        run.archive(r)
    shas = out["shas"]
    res: dict[str, Any] = {
        "row_counts": out["row_counts"],
        "licences": {k: {x: v[x] for x in ("hdx_dataset", "licence_id", "hdx_last_modified")} for k, v in licences.items()},
        "atlas": {},
    }
    for ds in DATASETS:
        rows = out["atlas"].get(ds)
        if ds not in want or rows is None:
            continue
        files = DATASET_FILES[ds]
        used = sorted({shas[f] for f in files if f in shas} | {hdx_shas[f] for f in files if f in hdx_shas})
        rights = {**RIGHTS, "hdx_records": [licences[f] for f in files if f in licences]}
        r = run.register(
            dataset=ds,
            frame=atlas_frame(rows),
            raw_sha256s=used,
            rights=rights,
            coverage_note=_COVERAGE[ds],
        )
        res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    for k in ("pre2010_overlap_check", "fatalities_repeats"):
        if k in out:
            res[k] = out[k]
    if "statements" in out:
        res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
        res["kg_report"] = out["kg_report"]
        if kg_available(run.engine):
            from reva.kg.model import register_properties

            register_properties(run.engine, kg_properties())
            res["kg"] = load_kg(
                run.engine,
                run.store,
                out["entities"],
                out["statements"],
                source_key=SOURCE_KEY,
                source_revision=f"psdata:{retrieved.date().isoformat()}",
                raw_sha256s=sorted(set(shas.values())),
                retrieved_at=retrieved,
                origin=run.origin,
            )
    return res
