"""Space: GCAT (Jonathan McDowell's General Catalog of Artificial Space Objects) -> launches and payloads.

Agent A-INFRA (V3). Source ``atlas.gcat`` (``source_key`` ``gcat.planet4589``).

Source and licence
------------------
GCAT, https://planet4589.org/space/gcat (J. C. McDowell). The index page states "GCAT is released under the
Creative Commons CC-BY licence ... Licence: CC-BY-4.0" and asks for the citation "McDowell, Jonathan C.,
2020. General Catalog of Artificial Space Objects, Release <x>, https://planet4589.org/space/gcat". The index
page is archived on every run and the licence marker is checked (a change is a rights event: the run is
refused); the release and "Data Update" date it states are recorded on every row.

Files (TSV, one request each): ``tsv/tables/orgs.tsv`` (organisations and states, with their GCAT codes),
``tsv/tables/sites.tsv`` (launch sites and the state each site record is attributed to),
``tsv/launch/launch.tsv`` (every launch: orbital, suborbital, missiles...), ``tsv/cat/satcat.tsv`` (the
standard object catalogue: objects that reached orbit).

Country codes
-------------
GCAT uses its own state codes (``US``, ``SU``, ``F``, ``J``, ``UK``, ``GUF``...). They are mapped through the
alias scheme ``gcat`` (``registry/atlas/geo_aliases/a_infra.yaml``, one evidence row of ``orgs.tsv`` per
code). Historical states (``SU`` USSR, ``CSSR``/``CSFR`` Czechoslovakia, ``HKUK`` British Hong Kong, ``TTPI``
Trust Territory of the Pacific Islands), intergovernmental organisations (``I-ESA``, ``I-INT``...) and the
pseudo-state ``EARTH`` (ocean sites) stay unmapped (``iso3`` null; ``entity_qid`` set for the USSR and
Czechoslovakia). A site's ``StateCode`` is the state GCAT attributes that site record to for its period
(Baikonur: ``NIIP-5``/SU before 1989, ``GIK-5``/RU after; Kourou: ``GUF``), not necessarily the territory's
current sovereign.

Derived counts (all ``derived: count`` with the formula; absent row = no record, not zero)
------------------------------------------------------------------------------------------
``launches`` (calendar year of ``Launch_Date``); an *orbital launch attempt* is a ``launch.tsv`` row whose
``LaunchCode`` category (first letter) is ``O`` (orbital) or ``D`` (deep space) and whose outcome (second
letter) is ``S``/``F``/``U`` (pad explosions ``E`` and pad aborts ``A`` are not launches):

* ``ORBITAL_LAUNCH_ATTEMPTS|by=site_state`` / ``ORBITAL_LAUNCH_SUCCESSES|by=site_state`` (outcome ``S``;
  GCAT codes a launch success fraction below 0.75 as ``F``) by the ``StateCode`` of the ``Launch_Site``;
* ``ORBITAL_LAUNCH_ATTEMPTS|by=agency_state``: by the ``StateCode`` of the launch agency (``Agency``; a
  composite ``A/B`` agency counts only when every part has the same state, else ``multi:<codes>``);
* ``ORBITAL_LAUNCH_SITES_USED|by=site_state``: distinct ``Launch_Site`` with >= 1 attempt that year.

``payloads`` (``satcat.tsv`` objects whose ``Type`` starts with ``P`` = payload; year of ``LDate``):

* ``PAYLOADS_CATALOGUED|by=owner_state``: by the payload's ``State`` (the country/state of the
  owner/operator, GCAT column definition);
* ``PAYLOADS_LAUNCHED_FROM|partner=<ISO3>`` (dyadic, owner state -> site state, different states): payloads
  of the owner state launched from a site attributed to another state (``Launch_Tag`` -> ``launch.tsv`` ->
  ``Launch_Site`` -> ``sites.tsv``); an unmapped site state is folded as ``partner_code``;
* ``PAYLOADS_STATUS_IN_ORBIT|by=owner_state`` (snapshot of the GCAT data update day): payloads whose current
  phase ``Status`` is ``O`` ("in orbit", free flight). This includes dead satellites: it is not "active".

Failed-to-orbit payloads (``ftocat``) and auxiliary objects (``auxcat``) are not counted (not fetched).
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import date
from typing import Any

from reva.adapters.connectors import atlas_infra_common as ic
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

NAME = "atlas.gcat"
VERSION = "1.0.0"
SOURCE_KEY = "gcat.planet4589"
ATLAS_SOURCE = "gcat"
DATASETS = ("launches", "payloads")
PARSER_VERSION = "atlas_gcat/1"
HOST = "planet4589.org"
BASE = f"https://{HOST}/space/gcat/"
INDEX_URL = BASE + "index.html"
FILES = {
    "orgs.tsv": BASE + "tsv/tables/orgs.tsv",
    "sites.tsv": BASE + "tsv/tables/sites.tsv",
    "launch.tsv": BASE + "tsv/launch/launch.tsv",
    "satcat.tsv": BASE + "tsv/cat/satcat.tsv",
}
LICENCE_MARKER = "CC-BY-4.0"
MAX_BYTES = 60_000_000
#: truncation guards (full files are far above these; tests on trimmed fixtures lower them)
MIN_LAUNCHES = 50_000
MIN_OBJECTS = 50_000
RIGHTS = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "McDowell, Jonathan C., 2020. General Catalog of Artificial Space Objects (GCAT), "
    "https://planet4589.org/space/gcat",
}
#: Wikidata QIDs of unmapped historical GCAT states (entity_qid column; not crosswalk countries).
HISTORICAL_QIDS = {"SU": "Q15180", "CSSR": "Q33946", "CSFR": "Q33946"}
_RELEASE = re.compile(r"GCAT Release\s+([0-9.]+)\s*\(([^)]*)\)\s*\|\s*Data Update\s+(\d{4}) (\w{3})\s+(\d{1,2})")
_MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}


def _err(msg: str) -> SourceResponseError:
    return SourceResponseError(msg, retryable=False, kind="schema_drift")


def code_digest() -> str:
    return ic.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Jonathan C. McDowell, General Catalog of Artificial Space Objects (GCAT), planet4589.org",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); cite: {RIGHTS['attribution']}",
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=tuple(f"gcat:{f}" for f in FILES),
        cadence_seconds=30 * ic.DAY,
        planned_requests=1 + len(FILES),
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        notes="Launches by site/agency state and payloads by owner state per year; owner->site-state pairs.",
    )


def parse_index(body: bytes) -> dict[str, Any]:
    text = re.sub(r"<[^>]*>", " ", body.decode("utf-8", "replace"))
    text = re.sub(r"\s+", " ", text)
    if LICENCE_MARKER not in text:
        raise SourceResponseError("GCAT index no longer states CC-BY-4.0", retryable=False, kind="licence")
    m = _RELEASE.search(text)
    if not m:
        raise _err("GCAT index: release / data update line not found")
    upd = date(int(m.group(3)), _MONTHS[m.group(4)], int(m.group(5)))
    return {"release": m.group(1), "release_date": m.group(2).strip(), "data_update": upd.isoformat(), "_day": upd}


def _year(s: str) -> int | None:
    y = s[:4]
    return int(y) if y.isdigit() and 1900 <= int(y) <= 2100 else None


class States:
    """GCAT state code -> (iso3 | None, entity_qid | None)."""

    def __init__(self, orgs: list[dict[str, str]]) -> None:
        self.orgs = {r["Code"]: r for r in orgs}

    @staticmethod
    def iso3(code: str) -> str | None:
        from reva.atlas.tables import country_iso3

        return country_iso3(code, "gcat") if code else None

    def agency_state(self, agency: str) -> str:
        parts = [p for p in agency.split("/") if p]
        states = {self.orgs[p]["StateCode"] for p in parts if p in self.orgs}
        if len(states) == 1 and all(p in self.orgs for p in parts):
            return states.pop()
        if not states:
            return f"unknown_agency:{agency}"
        return "multi:" + "+".join(sorted(states))


def build(
    files: dict[str, ic.Fetched], meta: dict[str, Any], wanted: tuple[str, ...] = DATASETS
) -> tuple[dict[str, ic.RowBuilder], dict[str, Any]]:
    orgs = ic.read_tsv(files["orgs.tsv"].body)
    sites = {r["Site"]: r for r in ic.read_tsv(files["sites.tsv"].body)}
    launches = ic.read_tsv(files["launch.tsv"].body)
    for name, rows, need in (
        ("orgs.tsv", orgs, {"Code", "StateCode", "Type"}),
        ("sites.tsv", list(sites.values()), {"Site", "StateCode"}),
        ("launch.tsv", launches, {"Launch_Tag", "Launch_Date", "Launch_Site", "Agency", "LaunchCode"}),
    ):
        if not rows or not need <= set(rows[0]):
            raise _err(f"{name} lacks columns {sorted(need - set(rows[0] if rows else {}))}")
    if len(launches) < MIN_LAUNCHES:
        raise _err(f"launch.tsv has only {len(launches)} rows (truncated?)")
    st = States(orgs)
    base = {"gcat_release": meta["release"], "gcat_data_update": meta["data_update"], "licence": "CC BY 4.0"}
    out: dict[str, ic.RowBuilder] = {}
    report: dict[str, Any] = {"gcat": {k: v for k, v in meta.items() if not k.startswith("_")}}
    site_state = {k: v["StateCode"] for k, v in sites.items()}

    def put(rb: ic.RowBuilder, ind: str, code: str, period: tuple[date, date], v: int, unit: str, f: ic.Fetched,
            sel: dict[str, Any], flags: dict[str, Any]) -> None:  # fmt: skip
        rb.count(indicator=ind, provider_geo=code, iso3=st.iso3(code), period=period, value=v, unit=unit,
                 raw_sha256=f.sha256, selector={"file": f.name, "aggregate": "count", "where": sel}, flags=flags,
                 entity_qid=HISTORICAL_QIDS.get(code))  # fmt: skip

    if "launches" in wanted:
        lf = files["launch.tsv"]
        att: dict[tuple[str, int], int] = defaultdict(int)
        suc: dict[tuple[str, int], int] = defaultdict(int)
        ag: dict[tuple[str, int], int] = defaultdict(int)
        used: dict[tuple[str, int], set[str]] = defaultdict(set)
        unknown_site = 0
        n = 0
        for r in launches:
            lc = r["LaunchCode"]
            if lc[:1] not in ("O", "D") or lc[1:2] not in ("S", "F", "U"):
                continue
            y = _year(r["Launch_Date"])
            if y is None:
                raise _err(f"launch.tsv {r['Launch_Tag']}: no year in Launch_Date {r['Launch_Date']!r}")
            n += 1
            sc = site_state.get(r["Launch_Site"])
            if sc is None:
                unknown_site += 1
                sc = f"unknown_site:{r['Launch_Site']}"
            att[(sc, y)] += 1
            if lc[1] == "S":
                suc[(sc, y)] += 1
            used[(sc, y)].add(r["Launch_Site"])
            ag[(st.agency_state(r["Agency"]), y)] += 1
        rb = ic.RowBuilder(ATLAS_SOURCE, "launches")
        lk = [files["sites.tsv"], files["orgs.tsv"]]
        o_rule = "LaunchCode first letter O or D and second letter S, F or U"
        for (sc, y), v in sorted(att.items()):
            w = {"site_state": sc, "year": y, "rule": o_rule}
            put(rb, "ORBITAL_LAUNCH_ATTEMPTS|by=site_state", sc, ic.year_span(y), v, "launches", lf, w,
                ic.derived(f"count of launch.tsv rows ({o_rule}) whose Launch_Site has sites.tsv StateCode = "
                           "the state, by year of Launch_Date", inputs=lk, **base))  # fmt: skip
            put(rb, "ORBITAL_LAUNCH_SUCCESSES|by=site_state", sc, ic.year_span(y), suc.get((sc, y), 0), "launches",
                lf, {**w, "outcome": "S"},
                ic.derived("as ORBITAL_LAUNCH_ATTEMPTS|by=site_state with LaunchCode second letter S", inputs=lk,
                           **base))  # fmt: skip
            put(rb, "ORBITAL_LAUNCH_SITES_USED|by=site_state", sc, ic.year_span(y), len(used[(sc, y)]), "sites", lf,
                w, ic.derived("count of distinct Launch_Site among the orbital launch attempts of that state and year",
                              inputs=lk, sites=sorted(used[(sc, y)]), **base))  # fmt: skip
        for (ac, y), v in sorted(ag.items()):
            put(rb, "ORBITAL_LAUNCH_ATTEMPTS|by=agency_state", ac, ic.year_span(y), v, "launches", lf,
                {"agency_state": ac, "year": y, "rule": o_rule},
                ic.derived(f"count of launch.tsv rows ({o_rule}) whose Agency has orgs.tsv StateCode = the state "
                           "(composite agencies only when all parts agree), by year of Launch_Date", inputs=lk,
                           **base))  # fmt: skip
        out["launches"] = rb
        report["launches"] = {"orbital_attempts": n, "unknown_site": unknown_site}

    if "payloads" in wanted:
        sat = ic.read_tsv(files["satcat.tsv"].body)
        if not sat or not {"Type", "State", "LDate", "Launch_Tag", "Status"} <= set(sat[0]):
            raise _err("satcat.tsv lacks expected columns")
        if len(sat) < MIN_OBJECTS:
            raise _err(f"satcat.tsv has only {len(sat)} rows (truncated?)")
        sf = files["satcat.tsv"]
        launch_site = {r["Launch_Tag"]: r["Launch_Site"] for r in launches}
        per: dict[tuple[str, int], int] = defaultdict(int)
        pair: dict[tuple[str, str, int], int] = defaultdict(int)
        in_orbit: dict[str, int] = defaultdict(int)
        no_launch = 0
        npay = 0
        for r in sat:
            if not r["Type"].startswith("P"):
                continue
            y = _year(r["LDate"])
            if y is None:
                raise _err(f"satcat.tsv {r.get('JCAT')}: no year in LDate {r['LDate']!r}")
            npay += 1
            own = r["State"] or "unknown"
            per[(own, y)] += 1
            if r["Status"] == "O":
                in_orbit[own] += 1
            site = launch_site.get(r["Launch_Tag"])
            sc = site_state.get(site) if site else None
            if sc is None:
                no_launch += 1
                continue
            a, b = st.iso3(own), st.iso3(sc)
            same = (a is not None and a == b) or own == sc
            if not same:
                pair[(own, sc, y)] += 1
        rb = ic.RowBuilder(ATLAS_SOURCE, "payloads")
        for (own, y), v in sorted(per.items()):
            put(rb, "PAYLOADS_CATALOGUED|by=owner_state", own, ic.year_span(y), v, "payloads", sf,
                {"State": own, "year": y, "Type": "P*"},
                ic.derived("count of satcat.tsv objects with Type starting 'P' (payload) and State = the state, by "
                           "year of LDate (objects that reached orbit; failed-to-orbit payloads not included)",
                           **base))  # fmt: skip
        for (own, sc, y), v in sorted(pair.items()):
            p = ic.Partner(st.iso3(sc), sc)
            put(rb, f"PAYLOADS_LAUNCHED_FROM|{p.dim()}", own, ic.year_span(y), v, "payloads", sf,
                {"State": own, "site_state": sc, "year": y, "Type": "P*"},
                ic.derived("count of satcat.tsv payloads (Type 'P*') with State = the owner state whose Launch_Tag's "
                           "launch.tsv Launch_Site has sites.tsv StateCode = the partner state (a different state), by "
                           "year of LDate", inputs=[files["launch.tsv"], files["sites.tsv"]], direction="owner->site",
                           **p.flags(), **base))  # fmt: skip
        snap = ic.day_span(meta["_day"])
        for own, v in sorted(in_orbit.items()):
            put(rb, "PAYLOADS_STATUS_IN_ORBIT|by=owner_state", own, snap, v, "payloads", sf,
                {"State": own, "Status": "O", "Type": "P*"},
                ic.derived("count of satcat.tsv payloads (Type 'P*') with State = the state and Status = 'O' (in orbit, "
                           "free flight) at the GCAT data update; includes inactive payloads", snapshot=True,
                           **base))  # fmt: skip
        out["payloads"] = rb
        report["payloads"] = {"payloads": npay, "without_launch_site": no_launch, "owner_site_pairs": len(pair)}
    return out, report


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    idx = await ic.fetch_archived(
        run, INDEX_URL, name="index.html", key="gcat:index", media_types=("text/html",), max_bytes=2_000_000
    )
    meta = parse_index(idx.body)
    need = ["orgs.tsv", "sites.tsv", "launch.tsv"] + (["satcat.tsv"] if "payloads" in wanted else [])
    files = {}
    for f in need:
        files[f] = await ic.fetch_archived(
            run, FILES[f], name=f, key=f"gcat:{f}", media_types=("text/tab-separated-values", "text/plain"),
            max_bytes=MAX_BYTES,
        )  # fmt: skip
    builders, report = build(files, meta, wanted)
    tables = []
    for ds in wanted:
        rb = builders[ds]
        shas = [idx.sha256] + [files[f].sha256 for f in need if f != "satcat.tsv" or ds == "payloads"]
        out = run.register(
            dataset=ds,
            frame=rb.frame(),
            raw_sha256s=shas,
            coverage_note=(
                f"GCAT release {meta['release']} (data update {meta['data_update']}): {len(rb.rows)} derived counts; "
                f"unmapped GCAT codes (historical states, IGOs, multi-agency) kept: "
                f"{', '.join(u['provider_geo'] for u in ic.unmapped_summary(rb.rows)[:30])}"
            ),
            rights={**RIGHTS, "source_key": SOURCE_KEY},
        )
        tables.append(
            {
                "table": out["dataset_key"].removeprefix("atlas."),
                "rows": out["row_count"],
                "created": out["created"],
                "artifact_sha256": out["artifact_sha256"],
                "unmapped_provider_geo": [u["provider_geo"] for u in out.get("unmapped_provider_geo") or []],
            }
        )
    return {"ok": True, "tables": tables, "report": report}
