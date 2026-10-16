"""Resistance campaigns and contentious episodes: NAVCO 1.2 / 1.3 / 2.1 and MEC (agent A-CONTENTION, V3).

Sources and access (Harvard Dataverse, all **CC0 1.0** per each dataset version's ``license`` record, checked
on every run)
---------------------------------------------------------------------------------------------------------------
Erica Chenoweth and colleagues, *Nonviolent and Violent Campaigns and Outcomes* (NAVCO) data project and the
*Major Episodes of Contention* (MEC) project. Each dataset version is pinned; the connector fetches the
version record (licence, file list, sizes, codebook md5) and the tabular file (the Dataverse ``.tab`` export,
redirected to the Dataverse S3 bucket). Dataverse's md5 describes the *original* upload (xlsx/dta), so the
``.tab`` is checked against the version record's ``filesize`` instead; the codebook whose text is quoted below
must still be listed in the version with the pinned md5 (the quotes come from that exact file).

* ``n12`` NAVCO 1.2 (``doi:10.7910/DVN/0UZOTX`` v2.0, file 3657601 "NAVCO 1.2 Updated.tab"): 389 maximalist
  campaigns 1945-2013, outcome as of the end of 2014, with regime violence, security-force defection,
  peak participation, violent flank, external support. The file carries 473 fully blank rows (spreadsheet
  artefacts), skipped and counted.
* ``n13`` NAVCO 1.3 (``doi:10.7910/DVN/ON9XND`` v1.0, file 3769104 "NAVCO 1.3 List.tab"): 622 campaigns
  1900-2019 (list: method, goals, outcome as of 2019-12-31). Its ids are "Random numbers assigned" (codebook)
  and several rows share one id (phases, or -- id 408 -- two different campaigns): a row is keyed
  ``<id>-<BYEAR>`` (+ ``-<k>`` for the four (id, BYEAR) pairs that repeat, in file order).
* ``n21`` NAVCO 2.1 (``doi:10.7910/DVN/MHOXDV`` v2.1, file 3620879): 384 campaigns, 2,717 campaign-years
  1945-2013 (day-precision start/end, yearly primary method, goals, size, repression, fatalities, defection,
  progress, final success).
* ``mec`` MEC v3.0 (``doi:10.7910/DVN/JQWQNW`` v3.0, file 13992597 "MEC Data Release.tab"): 2,734 episodes
  1955-2018, category 2 = reformist / 4 = maximalist, day-precision dates, goals, size, regime violence,
  defection, outcome.

NAVCO 3.0 (event-level, 26 countries 1990-2012, xlsx per country with news lead text) is not ingested yet
(``docs/dev/v3_a_contention.md``).

Outputs
-------
* atlas tables (COMPUTED counts per country-year, exact, every row says how): ``navco.n13_cy``,
  ``navco.n21_cy``, ``mec.episodes_cy``. Provider location names map to crosswalk countries through the
  schemes ``navco_loc`` / ``mec_loc`` of ``registry/atlas/geo_aliases/a_contention.yaml``; names of the same
  country are counted together (``provider_geo`` = the crosswalk key, ``flags.provider_locations`` lists the
  names); unmapped names (USSR, Yugoslavia, ...) keep ``provider_geo`` = the provider name. Only
  country-years with at least one campaign/episode are listed (absence of a row is not a reported zero).
* KG: events ``navco12:<key>``, ``navco13:<key>``, ``navco21:<id>``, ``mec:<id>`` (kind ``event``) with
  location, target (verbatim), dates, method, goals, size, defection, coded outcome; state-repression events
  (``...:repression:<year>`` NAVCO 2.1, ``...:regime_violence`` NAVCO 1.2 / MEC) and coded-outcome entities
  (``...:outcome``), linked to their campaign with non-causal structural predicates (``contention:*``).
  ``navco12`` rows link to ``navco21`` campaigns through the shared NAVCO id (codebook 1.2: the id comes
  from NAVCO 2.1 and split phases "share a NAVCO ID and are coded as a single campaign in the NAVCO 2.1
  dataset").
* Causal claims (tier T1, expert-coded, CAUSCORE interface ``reva.causal.load_dataset_claims``):
  ``coded_outcome`` (cause = campaign, effect = its coded-outcome entity) and ``reaction_to`` (cause = the
  campaign, effect = the state repression / regime violence that answers it, per the codebook definitions
  quoted in :data:`CODEBOOK`). One complete delivery per dataset file; loaded when CAUSCORE's loader exists,
  else staged as JSON in ``run.options["claims_staging"]`` (``causdata_common.deliver_claims``).
* Event dates are Wikidata ``P580`` start time / ``P582`` end time / ``P585`` point in time (the dates the
  causal grader reads), with the provider's precision.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
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
from reva.kernel.errors import SourceResponseError

CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-contention-navco-1"
HOSTS = ("dataverse.harvard.edu", "dvn-cloud-iqss.s3.amazonaws.com")
VERSION_URL = "https://dataverse.harvard.edu/api/datasets/:persistentId/versions/{version}?persistentId={doi}"
FILE_URL = "https://dataverse.harvard.edu/api/access/datafile/{}"
TAB_MEDIA = ("text/tab-separated-values", "text/plain", "application/octet-stream")
LICENCE = "CC0 1.0"
LICENCE_URL = "http://creativecommons.org/publicdomain/zero/1.0"
CLAIM_TIER = "T1"


class ContentionParseError(SourceResponseError):
    pass


# =============================================================================== Dataverse files


@dataclass(frozen=True)
class DvFile:
    """One pinned Dataverse tabular file and the codebook its quotes come from."""

    key: str
    title: str
    doi: str
    version: str
    file_id: int
    file_name: str
    min_rows: int
    columns: tuple[str, ...]
    codebook_id: int
    codebook_name: str
    codebook_md5: str
    codebook_sha256: str
    citation: str

    @property
    def version_url(self) -> str:
        return VERSION_URL.format(version=self.version, doi=self.doi)

    @property
    def file_url(self) -> str:
        return FILE_URL.format(self.file_id)

    @property
    def revision(self) -> str:
        return f"{self.doi}@{self.version}#{self.file_id}"


_NAVCO1_COLS: tuple[str, ...] = (
    *("NAVCOID", "CAMPAIGN", "LOCATION", "BYEAR", "EYEAR", "TARGET", "SUCCESS", "LIMITED", "FAILURE"),
    *("ONGOING", "NONVIOL", "VIOL", "REGCHANGE", "FSELFDET", "SECESSION", "OTHER"),
)

FILES: dict[str, DvFile] = {
    "n12": DvFile(
        "n12",
        "NAVCO 1.2",
        "doi:10.7910/DVN/0UZOTX",
        "2.0",
        3657601,
        "NAVCO 1.2 Updated.tab",
        380,
        (*_NAVCO1_COLS, "PARTICIPATION", "REGVIOL", "DEFECT", "VIOLENTFLANK", "VIOLSIM", "REGAID", "STATESUP"),
        3621197,
        "NAVCO 1.2 Codebook.pdf",
        "c7b56af6082e09abf6b250bd1e154678",
        "6743dc6163167966dcdf92ce6a8f9b6d1bbcf6c40cc047b8f82030ebee25a30b",
        "Chenoweth, Erica; Shay, Christopher Wiley, 'NAVCO 1.2 Dataset', Harvard Dataverse, doi:10.7910/DVN/0UZOTX, V2",
    ),
    "n13": DvFile(
        "n13",
        "NAVCO 1.3",
        "doi:10.7910/DVN/ON9XND",
        "1.0",
        3769104,
        "NAVCO 1.3 List.tab",
        600,
        _NAVCO1_COLS,
        3769105,
        "NAVCO 1.3 Codebook List.pdf",
        "d5196bd992a93efa3ccfd9c492cb35bc",
        "8a265f0083a7541d353a54976e4648a8298c8386b3d5948a0f4da00620341af3",
        "Chenoweth, Erica; Shay, Christopher Wiley, 'List of Campaigns in NAVCO 1.3', Harvard Dataverse, "
        "doi:10.7910/DVN/ON9XND, V1",
    ),
    "n21": DvFile(
        "n21",
        "NAVCO 2.1",
        "doi:10.7910/DVN/MHOXDV",
        "2.1",
        3620879,
        "NAVCO2-1_ForPublication.tab",
        2600,
        (
            "camp_name",
            "id",
            "year",
            "location",
            "target",
            "status",
            "cyear",
            "start_date",
            "end_date",
            "prim_meth",
            "camp_goals",
            "camp_size",
            "total_part",
            "repression",
            "fatalities_high",
            "fatalities_low",
            "sec_defect",
            "success",
            "progress",
        ),
        3648624,
        "NAVCO_2.1_codebook.pdf",
        "680ed263cd9c856add89b8eeeae6eed0",
        "6fe8faa4c6ad45c74d61082d6aedc780d5440a7ace25747a63eaf338951c9a93",
        "Chenoweth, Erica; Shay, Christopher Wiley, 'NAVCO 2.1 Dataset', Harvard Dataverse, doi:10.7910/DVN/MHOXDV, "
        "V2.1",
    ),
    "n30": DvFile(
        "n30",
        "NAVCO 3.0",
        "doi:10.7910/DVN/INNYEO",
        "1.0",
        3612128,
        "navco3-0full.xlsx",
        100_000,
        ("country_name", "cowcode", "date", "actor_id", "verb_10", "tactical_choice", "st_posture"),
        3612135,
        "navco3-0codebook.pdf",
        "f2438e3acad36dbaab0c6151fce8f053",
        "20785f63ba9169af932f8c25a30cb71d62d0b65032a522f5fecae1255615e92f",
        "Chenoweth, Erica; Pinckney, Jonathan; Lewis, Orion A., 'NAVCO 3.0 Dataset', Harvard Dataverse, "
        "doi:10.7910/DVN/INNYEO, V1",
    ),
    "mec": DvFile(
        "mec",
        "MEC v3.0",
        "doi:10.7910/DVN/JQWQNW",
        "3.0",
        13992597,
        "MEC Data Release.tab",
        2600,
        (
            "id",
            "campaign",
            "location",
            "ccode",
            "byear",
            "eyear",
            "bdate",
            "edate",
            "category",
            "actor",
            "target",
            "success",
            "limited",
            "failure",
            "ongoing",
            "unknown",
            "viol",
            "nonviol",
            "tactics",
            "fselfdet",
            "regchange",
            "secession",
            "electoral",
            "electoraldescription",
            "social",
            "socialdescription",
            "economic",
            "economicdescription",
            "political",
            "politicaldescription",
            "other",
            "otherdescription",
            "numbers",
            "regviol",
            "defect",
            "radflank",
            "regaid",
            "statesup",
            "violsim",
        ),
        11543083,
        "MEC_Codebook_2024.docx",
        "4c314592b77409036f1d152791383d0c",
        "3d5b0133516cec2d5d5a6db7d4c62821a778fcca456c70ac30b1d773757e16bd",
        "Chenoweth, Erica; Kang, Sooyeon, 'Major Episodes of Contention', Harvard Dataverse, doi:10.7910/DVN/JQWQNW, V3",
    ),
}

#: Codebook text quoted verbatim (whitespace joined; typographic quotes/apostrophes as ASCII). Checked against
#: the pinned codebook files (sha256 in :data:`FILES`) on 2026-10-16 (``docs/dev/v3_a_contention.md``).
CODEBOOK: dict[str, dict[str, str]] = {
    "n12": {
        "SUCCESS": "Binary variable identifying whether the campaign achieved 100% of its stated goals within a "
        "year of the peak of activities. In most cases, outcome was achieved within a year of the campaign's peak. "
        'Some campaigns\' goals were achieved years after the "peak" of the struggle in terms of membership, but '
        "the success was a direct result of campaign activities. When such a direct link can be demonstrated, "
        "these campaigns are coded as successful.",
        "LIMITED": "Binary variable identifying whether the campaign achieved some of its stated goals within a year "
        "of the peak of activities. When a regime makes concessions to the campaign or reforms short of complete "
        "campaign success, such reforms are counted as limited success.",
        "FAILURE": "Binary variable identifying whether the campaign achieved none of its stated goals; includes "
        "campaigns that have been suppressed. Campaigns are coded as failures when they are ongoing as of the end "
        'of 2014 and are classified as "ongoing."',
        "ONGOING": "Binary variable identifying whether the campaign is ongoing and has not yet achieved its "
        "goals. Only when campaigns are ongoing and have achieved no stated objectives/outcomes by the end of 2014.",
        "REGVIOL": "Binary variable identifying whether the regime used violence to crack down on campaign.",
        "DEFECT": "Binary variable identifying whether the target's security forces defected or engaged in "
        "noncooperation during the conflict.",
        "EYEAR": "Year at which the campaign reaches its peak; i.e. the most members or watershed events. If data "
        "is unavailable for membership or events, this peak is considered to be the year in which the campaign "
        "ended",
        "NAVCOID": "they were split into two campaigns but share a NAVCO ID and are coded as a single campaign in "
        "the NAVCO 2.1 dataset",
        "PARTICIPATION": "Number of participants observed actively confronting opponent at highest point (peak "
        "event). This reports the largest participation reported at a watershed event. It does not estimate the "
        "cumulative number of participants.",
    },
    "n13": {
        "SUCCESS": "Outcome classification. 1=the campaign achieved 100% of its stated goals within a year of the "
        "peak of activities & the success was a direct result of campaign activities), 0=otherwise.",
        "LIMITED": "Outcome classification. 1=the campaign achieved some of its stated goals within a year of the "
        "peak of activities; the target makes concessions to the campaign or significant reforms short of "
        "complete campaign success), 0=otherwise.",
        "FAILURE": "Outcome classification. 1=the campaign achieved none of its stated goals and/or has been "
        "suppressed. 0=otherwise",
        "ONGOING": "Outcome classification. 1=campaign was ongoing as of 12/31/2019, 0=otherwise. Campaigns coded "
        "as ongoing are also coded as failures.",
        "EYEAR": "Year at which the campaign reaches its peak; i.e. the most members or watershed events. If data "
        "is unavailable for membership or events, this peak is considered to be the year in which the campaign "
        "ended",
        "LOCATION": "Geographic base of campaign (contemporary sovereign country name).",
    },
    "n21": {
        "repression": "This variable measures the most repressive episode or activity perpetrated by the state in "
        "response to campaign activity.",
        "success": "Dichotomous variable identifying whether the campaign achieved at least one stated, maximalist "
        "goal within 1 calendar year of its end date.",
        "progress": "Ordinal variable identifying whether the campaign achieved some or all of its stated overall "
        "political objectives.",
        "start_date": "First date on which campaign activity is observed",
        "end_date": "Last date on which campaign activity is observed",
    },
    "mec": {
        "SUCCESS": "Dichotomous variable identifying whether the campaign achieved 100% of its stated goals within a "
        "year of the peak of activities.",
        "LIMITED": "Dichotomous variable identifying whether the campaign achieved some of its stated goals within a "
        "year of the peak of activities. When a regime makes concessions to the campaign or reforms short of "
        "complete campaign success, such reforms are counted as limited success.",
        "FAILURE": "Dichotomous variable identifying whether the campaign achieved none of its stated goals; "
        "includes campaigns that have been suppressed. Note that campaigns that were coded as ongoing as of "
        "December 31, 2018 were also coded 1 for failure.",
        "ONGOING": "Dichotomous variable identifying whether the campaign is ongoing and has not yet achieved its "
        "stated objectives/goals, as of December 31, 2018.",
        "REGVIOL": "Dichotomous variable identifying whether the regime used violence to crack down on campaign.",
        "REGVIOL_TABLE": "Did the target use violence to retaliate against the campaign",
    },
}

N21_REPRESSION = {"0": "none", "1": "mild", "2": "moderate", "3": "extreme"}
N21_REPRESSION_DEF = {
    "1": "verbal or threatening action short of physical action; express intent to engage in conflict or threaten; "
    "use of economic fees and levies to increase costs on opposition; decline to cease ongoing conflict",
    "2": "physical or violent action aimed at coercing opponent; harassment and imprisonment of campaign members; "
    "no apparent intention to kill",
    "3": "physical action exhibiting intent to kill and violently silence opponents; torture or severe violence "
    "(such as severe beatings), which could easily kill someone",
}
N21_PROGRESS = {
    "0": "status quo",
    "1": "visible gains short of concessions",
    "2": "limited concession achieved",
    "3": "significant concessions achieved",
    "4": "complete success",
    "5": "campaign ends in failure",
}
N21_GOALS = {
    "0": "regime change",
    "1": "significant institutional reform",
    "2": "policy change",
    "3": "territorial secession",
    "4": "greater autonomy",
    "5": "anti-occupation",
}
N21_SIZE = {
    "0": "1-999",
    "1": "1,000-9,999",
    "2": "10,000-99,999",
    "3": "100,000-499,999",
    "4": "500,000-999,999",
    "5": ">= 1 million",
}
N21_CYEAR = {"0": "onset", "1": "ongoing", "2": "end year", "3": "post campaign"}
NAVCO1_GOALS = {
    "REGCHANGE": "regime change",
    "FSELFDET": "self-determination / anti-occupation",
    "SECESSION": "secession",
    "OTHER": "other",
}
MEC_GOALS = {
    "regchange": "regime change",
    "fselfdet": "independence from a foreign occupier",
    "secession": "secession",
    "electoral": "electoral",
    "social": "social",
    "economic": "economic",
    "political": "political",
    "other": "other",
}
MEC_CATEGORY = {"2": "reformist", "4": "maximalist"}
OUTCOMES = ("success", "limited", "failure", "ongoing")


# =============================================================================== parsing helpers


def check_version(meta_body: bytes, data_body: bytes, f: DvFile, *, extra_ids: Sequence[int] = ()) -> dict[str, Any]:
    """Licence, file presence/size and codebook md5 in the pinned Dataverse version record; raises on drift."""
    try:
        doc = json.loads(meta_body)
    except ValueError as e:
        raise ContentionParseError(
            f"{f.title}: version record is not JSON", retryable=False, kind="schema_drift"
        ) from e
    data = doc.get("data") or {}
    if f"{data.get('versionNumber')}.{data.get('versionMinorNumber')}" != f.version:
        raise ContentionParseError(
            f"{f.title}: Dataverse returned another version", retryable=False, kind="schema_drift"
        )
    lic = (data.get("license") or {}).get("name")
    if lic != LICENCE:
        raise ContentionParseError(f"{f.title}: licence changed: {lic!r}", retryable=False, kind="licence_change")
    files = {x["dataFile"]["id"]: x["dataFile"] for x in data.get("files", []) if "dataFile" in x}
    df = files.get(f.file_id)
    if df is None or df.get("filename") != f.file_name:
        raise ContentionParseError(
            f"{f.title}: data file not in the pinned version", retryable=False, kind="schema_drift"
        )
    if df.get("filesize") is not None and int(df["filesize"]) != len(data_body):
        raise ContentionParseError(
            f"{f.title}: {len(data_body)} bytes, version record says {df['filesize']}",
            retryable=False,
            kind="checksum",
        )
    if not df.get("tabularData", True) and df.get("md5"):  # a non-ingested upload: its md5 is the file's own
        md5 = hashlib.md5(data_body, usedforsecurity=False).hexdigest()
        if md5 != df["md5"]:
            raise ContentionParseError(
                f"{f.title}: md5 differs from the version record", retryable=False, kind="checksum"
            )
    cb = files.get(f.codebook_id)
    if cb is None or cb.get("md5") != f.codebook_md5:
        raise ContentionParseError(
            f"{f.title}: quoted codebook {f.codebook_name} (md5 {f.codebook_md5}) not in the pinned version",
            retryable=False,
            kind="schema_drift",
        )
    for i in extra_ids:
        if i not in files:
            raise ContentionParseError(f"{f.title}: file {i} not in the version", retryable=False, kind="schema_drift")
    return {
        "licence": lic,
        "version": f.version,
        "release_time": data.get("releaseTime"),
        "file_size": df.get("filesize"),
        "unf": df.get("UNF"),
        "original_md5": df.get("md5"),
        "codebook_md5": cb.get("md5"),
    }


@dataclass
class Table:
    """Rows of a Dataverse ``.tab`` export: ``rows[i]`` came from physical line ``lines[i]`` (header = line 1)."""

    rows: list[dict[str, str]]
    lines: list[int]
    blank_rows: int = 0

    def __iter__(self) -> Iterator[tuple[int, dict[str, str]]]:
        return iter(zip(self.lines, self.rows, strict=True))

    def __len__(self) -> int:
        return len(self.rows)


def read_tab(body: bytes, f: DvFile, *, strict: bool = True) -> Table:
    """Parse the tab export (UTF-8, strict), skip fully blank rows (counted), check the expected columns."""
    try:
        text_ = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ContentionParseError(f"{f.title}: not UTF-8", retryable=False, kind="schema_drift") from e
    rd = csv.reader(io.StringIO(text_, newline=""), delimiter="\t")
    try:
        header = next(rd)
    except StopIteration as e:
        raise ContentionParseError(f"{f.title}: empty file", retryable=False, kind="incomplete") from e
    missing = [c for c in f.columns if c not in header]
    if missing:
        raise ContentionParseError(f"{f.title}: missing columns {missing}", retryable=False, kind="schema_drift")
    rows: list[dict[str, str]] = []
    lines: list[int] = []
    blank = 0
    for rec in rd:
        if len(rec) != len(header):
            raise ContentionParseError(
                f"{f.title}: line {rd.line_num} has {len(rec)} fields, header {len(header)}",
                retryable=False,
                kind="schema_drift",
            )
        if not any(v.strip() for v in rec):
            blank += 1
            continue
        rows.append({h: v.strip() for h, v in zip(header, rec, strict=True)})
        lines.append(rd.line_num)
    if strict and len(rows) < f.min_rows:
        raise ContentionParseError(
            f"{f.title}: {len(rows)} rows < {f.min_rows} (truncated?)", retryable=False, kind="incomplete"
        )
    return Table(rows, lines, blank)


def int_val(v: str | None, *, what: str = "", unknown: Iterable[str] = ("", "-99", "NA", ".")) -> int | None:
    """``'4.0'`` -> 4; provider unknown codes -> None; a non-integral value is schema drift."""
    s = (v or "").strip()
    if s in set(unknown):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise ContentionParseError(f"{what}: not a number {s!r}", retryable=False, kind="schema_drift") from e
    if d != d.to_integral_value():
        raise ContentionParseError(f"{what}: not an integer {s!r}", retryable=False, kind="schema_drift")
    return int(d)


def flag01(v: str | None, *, what: str = "") -> bool | None:
    i = int_val(v, what=what)
    if i is None:
        return None
    if i not in (0, 1):
        raise ContentionParseError(f"{what}: binary variable is {v!r}", retryable=False, kind="schema_drift")
    return i == 1


def code_str(v: str | None, *, what: str = "", unknown: Iterable[str] = ("", "-99", "NA", ".")) -> str | None:
    i = int_val(v, what=what, unknown=unknown)
    return None if i is None else str(i)


def dec_text(v: str | None, *, unknown: Iterable[str] = ("", "-99", "NA", ".")) -> str | None:
    """Strict decimal string (``'1.4E7'`` -> ``'14000000'``) or None for unknown codes."""
    s = (v or "").strip()
    if s in set(unknown):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    out = format(d, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return "0" if out in ("-0", "") else out


def parse_ymd(v: str | None) -> date | None:
    """``1991-1-5`` / ``2001-12-23`` -> date; anything else -> None."""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", (v or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return (out or "x")[:60]


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# =============================================================================== places


HISTORICAL_NAME_COW: dict[str, int] = {
    # provider location name -> COW code whose dated Wikidata QIDs are in registry/entities/historical_states_qids.json
    "USSR": 365,
    "Czechoslovakia": 315,
    "East Germany": 265,
    "Germany East": 265,
    "Germany West": 260,
    "South Vietnam": 817,
    "Yemen Arab Republic": 678,
    "Yemen (North Yemen)": 678,
    "North Yemen": 678,
    "Yemen People's Republic": 680,
    "South Yemen": 680,
    "Yugoslavia": 345,
    "Serbia and Montenegro": 345,
    "Ottoman Empire": 640,
}


@dataclass(frozen=True)
class Place:
    name: str
    iso3: str | None
    node: str
    how: str  # qid | iso3_node_qid_unresolved | historical_qid | provider_name


class Places:
    """Provider location name (+ year) -> crosswalk key (geo scheme) and KG node.

    KG node: the country's Wikidata QID (``registry/entities/country_qids.json``), else ``iso3:<KEY>``; a
    historical state's verified QID (A-HISTECON's ``historical_states_qids.json``, dated COW entries) when the
    name is listed in :data:`HISTORICAL_NAME_COW` and the year falls in an entry; else a provider place entity
    ``<ns>:place:<slug>`` labelled with the name.
    """

    def __init__(self, scheme: str, ns: str, qid_for_iso3: Mapping[str, str] | None = None) -> None:
        from reva.adapters.connectors.atlas_geopol_common import load_historical_qids
        from reva.atlas.tables import country_iso3

        self.scheme = scheme
        self.ns = ns
        self._iso = country_iso3
        self.qid_for_iso3 = dict(load_qid_for_iso3() if qid_for_iso3 is None else qid_for_iso3)
        self.hist = load_historical_qids()
        self.extra: dict[str, KgEntity] = {}

    def iso3(self, name: str) -> str | None:
        return self._iso(name, self.scheme) if name else None

    def resolve(self, name: str, year: int | None) -> Place:
        iso3 = self.iso3(name)
        if iso3:
            q = self.qid_for_iso3.get(iso3)
            if q:
                return Place(name, iso3, q, "qid")
            node = f"iso3:{iso3}"
            self.extra.setdefault(
                node,
                KgEntity(
                    qid=node,
                    kind="country_code",
                    labels={"en": f"{iso3} (ISO 3166-1 alpha-3)"},
                    descriptions={
                        "en": "crosswalk country without a Wikidata QID in registry/entities/country_qids.json"
                    },
                ),
            )
            return Place(name, iso3, node, "iso3_node_qid_unresolved")
        cow = HISTORICAL_NAME_COW.get(name)
        if cow is not None and year is not None:
            for h in self.hist:
                if (
                    h["code"] == cow
                    and (h["from"] is None or h["from"] <= year)
                    and (h["to"] is None or year <= h["to"])
                ):
                    return Place(name, None, h["qid"], "historical_qid")
        node = f"{self.ns}:place:{slug(name)}"
        self.extra.setdefault(
            node,
            KgEntity(
                qid=node,
                kind="place_name",
                labels={"en": name},
                descriptions={
                    "en": f"location as named by the provider ({self.ns}); not mapped to a crosswalk country"
                },
            ),
        )
        return Place(name, None, node, "provider_name")

    def entities(self) -> list[KgEntity]:
        return [self.extra[k] for k in sorted(self.extra)]


# =============================================================================== atlas counting


class Counts:
    """Exact counts per (geo key, year, indicator) with the contributing provider rows."""

    def __init__(self) -> None:
        self.n: dict[tuple[str, int, str], set[str]] = defaultdict(set)
        self.lines: dict[tuple[str, int, str], set[int]] = defaultdict(set)
        self.names: dict[tuple[str, int], set[str]] = defaultdict(set)
        self.active: set[tuple[str, int]] = set()

    def add(self, geo: str, year: int, indicator: str, item: str, line: int, name: str) -> None:
        self.n[(geo, year, indicator)].add(item)
        self.lines[(geo, year, indicator)].add(line)
        self.names[(geo, year)].add(name)

    def touch(self, geo: str, year: int, name: str) -> None:
        self.active.add((geo, year))
        self.names[(geo, year)].add(name)


def count_rows(
    counts: Counts,
    *,
    source: str,
    dataset: str,
    indicators: Mapping[str, str],
    raw_sha256: str,
    file: str,
    method: str,
    computed_from: str,
    extra_flags: Mapping[str, Any] | None = None,
    zero_fill: bool = True,
    zero_lines: Mapping[tuple[str, int], int] | None = None,
    max_items: int = 200,
) -> AtlasRows:
    """Atlas rows: every indicator for every active (geo, year) -- zeros are computed from the same complete
    coding of the listed campaigns (``zero_fill``); geo keys are crosswalk keys or provider names (unmapped).
    ``flags.items`` lists up to ``max_items`` counted provider ids (0: none, the count and selector suffice)."""
    from reva.atlas.tables import json_obj, year_period

    out = AtlasRows()
    keys = sorted(counts.active)
    for geo, year in keys:
        iso3 = geo if not geo.startswith("name:") else None
        pgeo = geo if iso3 else geo[len("name:") :]
        names = sorted(counts.names[(geo, year)])
        start, end = year_period(year)
        for ind, definition in indicators.items():
            items = counts.n.get((geo, year, ind), set())
            if not items and not zero_fill:
                continue
            lines = sorted(counts.lines.get((geo, year, ind), set()))
            flags: dict[str, Any] = {
                "computed_from": computed_from,
                "method": method,
                "definition": definition,
                "provider_locations": names,
            }
            if max_items:
                flags["items"] = sorted(items, key=lambda s: (len(s), s))[:max_items]
                if len(items) > max_items:
                    flags["items_total"] = len(items)
            if iso3 is None:
                flags["unmapped_location"] = pgeo
            if extra_flags:
                flags.update(extra_flags)
            out.add(
                {
                    "source": source,
                    "dataset": dataset,
                    "indicator": ind,
                    "concept_id": None,
                    "iso3": iso3,
                    "provider_geo": pgeo,
                    "entity_qid": None,
                    "period_start": start,
                    "period_end": end,
                    "value": str(len(items)),
                    "value_text": None,
                    "unit": "count",
                    "flags": json_obj(flags),
                    "raw_sha256": raw_sha256,
                    "selector": _selector(file, year, lines, (zero_lines or {}).get((geo, year))),
                }
            )
    return out


def _selector(file: str, year: int, lines: Sequence[int], zero_line: int | None) -> str:
    from reva.atlas.tables import json_obj

    if lines:
        return json_obj(file=file, lines=list(lines[:100]), lines_total=len(lines), year=year)
    if zero_line is not None:
        return json_obj(file=file, line=zero_line, year=year, note="provider row coding no event for the country-year")
    return json_obj(file=file, year=year, note="zero: none of the rows counted for this country-year matches")


def geo_key(places: Places, name: str) -> str:
    iso3 = places.iso3(name)
    return iso3 if iso3 else f"name:{name}"


# =============================================================================== causal claims


def claim(
    *,
    key: str,
    cause: str,
    effect: str,
    relation: str,
    f: DvFile,
    source_key: str,
    raw_sha256: str,
    selector: Mapping[str, Any],
    coded_value: str,
    codebook_term: str,
    scope: Mapping[str, Any] | None = None,
) -> Any:
    """One T1 claim with exactly the fields of ``reva.causal.DatasetClaim`` (via CAUSDATA's ``ClaimRecord``).

    REVA asserts nothing: the asserting source is the dataset row (``selector`` + ``raw_sha256``; the coded values
    are listed in ``selector["coded"]``) and ``codebook_term`` names the verbatim codebook definition
    (:data:`CODEBOOK`) that gives the coded relation its meaning."""
    from reva.adapters.connectors.causdata_common import ClaimRecord

    prefix = f"{source_key}:{f.key}:"
    if not key.startswith(prefix):
        raise ValueError(f"claim key {key!r} must start with {prefix!r}")
    if codebook_term not in codebook_definitions(f):
        raise ValueError(f"{f.title}: no codebook definition {codebook_term!r}")
    return ClaimRecord(
        cause_id=cause,
        effect_id=effect,
        relation=relation,
        selector={**dict(selector), "coded": coded_value},
        raw_sha256=raw_sha256,
        codebook_term=codebook_term,
        source_claim_id=key[len(prefix) :],
        scope=dict(scope or {}),
        tier=CLAIM_TIER,
    )


def codebook_definitions(f: DvFile) -> dict[str, str]:
    if f.key == "mmp":
        from reva.adapters.connectors.atlas_massmob import MANUAL

        return dict(MANUAL)
    return dict(CODEBOOK[f.key])


def codebook_record(f: DvFile) -> dict[str, Any]:
    """``reva.causal.Codebook`` fields: the pinned codebook file (Dataverse id, md5, sha256) and its verbatim
    definitions."""
    return {
        "name": f"{f.title} codebook ({f.codebook_name})",
        "version": f"{f.doi} v{f.version}; file {f.codebook_id} md5 {f.codebook_md5} sha256 {f.codebook_sha256}",
        "url": FILE_URL.format(f.codebook_id),
        "definitions": codebook_definitions(f),
    }


def delivery(
    f: DvFile, dataset: str, claims: Sequence[Any], *, source_key: str, raw_sha256: str, retrieved_at: Any, origin: str
) -> Any:
    """Exactly the arguments of ``reva.causal.load_dataset_claims`` for one dataset file (complete delivery)."""
    from reva.adapters.connectors.causdata_common import ClaimDelivery

    return ClaimDelivery(
        source_key=source_key,
        dataset=dataset,
        dataset_revision=f.revision,
        codebook=codebook_record(f),
        raw_sha256s=[raw_sha256],
        retrieved_at=retrieved_at,
        origin=origin,
        claims=list(claims),
        complete=True,
    )


def deliver(run: Any, deliveries: Sequence[Any]) -> dict[str, Any]:
    """Load through CAUSCORE's ``load_dataset_claims`` when available, else stage the deliveries as JSON files in
    ``run.options["claims_staging"]`` (CAUSDATA's format, loadable with ``causdata_common.load_staged``), else
    report ``causal_unavailable`` (``causdata_common.deliver_claims``)."""
    from reva.adapters.connectors.causdata_common import deliver_claims

    staging = (run.options or {}).get("claims_staging")
    if staging and str(getattr(run.settings, "reva_env", "") or "") == "production":
        raise ContentionParseError("claims_staging is a development option", retryable=False, kind="policy")
    return deliver_claims(run, deliveries)


# =============================================================================== KG properties

#: pid -> (value kind, reva predicate, en label, fr label, inverse name, inverse en label). Structural and
#: descriptive predicates only: causal claims never go into kg.statement (they are staged for kg.causal_claim).
CONTENTION_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "contention:event_type": ("string", "contention_event_type", "event type (contention datasets)", "type d'événement (contestation)", None, None),
    "contention:location": ("item", "contention_location", "location (country as coded by the dataset)", "lieu (pays codé par la base)", "contention_location_of", "location of contentious event"),
    "contention:location_text": ("string", "contention_location_text", "sub-national location (provider text)", "lieu infranational (texte du fournisseur)", None, None),
    "contention:target": ("string", "contention_target", "target of the campaign (provider text)", "cible de la campagne (texte du fournisseur)", None, None),
    "contention:actor": ("string", "contention_actor", "campaign actor (provider text)", "acteur de la campagne (texte du fournisseur)", None, None),
    "contention:peak_or_end_year": ("time", "contention_peak_or_end_year", "peak (or end) year (NAVCO EYEAR)", "année du pic (ou de fin) (NAVCO EYEAR)", None, None),
    "contention:primary_method": ("string", "contention_primary_method", "primary method of resistance (as coded)", "méthode principale de résistance (codée)", None, None),
    "contention:goal": ("string", "contention_goal", "stated goal category (as coded)", "catégorie d'objectif déclaré (codée)", None, None),
    "contention:category": ("string", "contention_category", "episode category (MEC)", "catégorie d'épisode (MEC)", None, None),
    "contention:peak_participation": ("quantity", "contention_peak_participation", "participants at the peak event (as coded)", "participants à l'événement culminant (codé)", None, None),
    "contention:size_category": ("string", "contention_size_category", "campaign size category (as coded)", "catégorie de taille de la campagne (codée)", None, None),
    "contention:coded_outcome": ("string", "contention_coded_outcome", "outcome as coded by the dataset", "issue codée par la base", None, None),
    "contention:progress": ("string", "contention_progress", "campaign progress in the year (NAVCO 2.1)", "progrès de la campagne dans l'année (NAVCO 2.1)", None, None),
    "contention:repression_level": ("string", "contention_repression_level", "state repression level (as coded)", "niveau de répression étatique (codé)", None, None),
    "contention:regime_violence": ("string", "contention_regime_violence", "regime violence against the campaign (as coded)", "violence du régime contre la campagne (codée)", None, None),
    "contention:security_force_defection": ("string", "contention_security_force_defection", "security-force defection (as coded)", "défection des forces de sécurité (codée)", None, None),
    "contention:regime_inflicted_fatalities": ("quantity", "contention_regime_inflicted_fatalities", "regime-inflicted fatalities in the year (NAVCO 2.1)", "morts infligées par le régime dans l'année (NAVCO 2.1)", None, None),
    "contention:violent_flank": ("string", "contention_violent_flank", "violent / radical flank (as coded)", "aile violente / radicale (codée)", None, None),
    "contention:external_support_campaign": ("string", "contention_external_support_campaign", "overt external state support to the campaign (as coded)", "soutien étatique extérieur à la campagne (codé)", None, None),
    "contention:external_support_regime": ("string", "contention_external_support_regime", "overt external military aid to the target regime (as coded)", "aide militaire extérieure au régime (codée)", None, None),
    "contention:simultaneous_violent_campaign": ("string", "contention_simultaneous_violent_campaign", "another violent campaign active at the same time (as coded)", "autre campagne violente simultanée (codée)", None, None),
    "contention:provider_id": ("string", "contention_provider_id", "identifier in the provider dataset", "identifiant dans la base du fournisseur", None, None),
    "contention:outcome_of": ("item", "contention_outcome_of", "coded outcome of (campaign)", "issue codée de (campagne)", "contention_has_coded_outcome", "has coded outcome"),
    "contention:directed_at": ("item", "contention_directed_at", "state action directed at (campaign / protest)", "action étatique visant (campagne / manifestation)", "contention_targeted_by", "targeted by state action"),
    "contention:same_campaign_as": ("item", "contention_same_campaign_as", "same campaign as (shared NAVCO id, codebook)", "même campagne que (identifiant NAVCO partagé)", "contention_same_campaign_as", "same campaign as (shared NAVCO id, codebook)"),
    "contention:phase_of": ("item", "contention_phase_of", "phase of campaign (shared NAVCO id, codebook)", "phase de la campagne (identifiant NAVCO partagé)", "contention_has_phase", "has phase"),
    "contention:protester_demand": ("string", "contention_protester_demand", "protester demand (MMP category)", "revendication des manifestants (catégorie MMP)", None, None),
    "contention:state_response": ("string", "contention_state_response", "state response (MMP category)", "réponse de l'État (catégorie MMP)", None, None),
    "contention:protester_violence": ("string", "contention_protester_violence", "protester violence (MMP)", "violence des manifestants (MMP)", None, None),
    "contention:protester_identity": ("string", "contention_protester_identity", "protester identity (provider text)", "identité des manifestants (texte du fournisseur)", None, None),
    "contention:participants_reported": ("string", "contention_participants_reported", "participants as reported (provider text)", "participants rapportés (texte du fournisseur)", None, None),
    "contention:participants_category": ("string", "contention_participants_category", "participants category (MMP)", "catégorie de participants (MMP)", None, None),
}  # fmt: skip


def kg_properties() -> list[Any]:
    from reva.kg.model import KgProperty

    return [
        KgProperty(
            pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv, inverse_label_en=ile
        )
        for p, (k, rp, le, lf, inv, ile) in CONTENTION_PROPERTIES.items()
    ]


#: Wikidata time properties (WDHARVEST's allow-list definitions): start time, end time, point in time -- the event
#: dates CAUSCORE's grader reads for the temporal check (docs/dev/v3_causal_interface.md).
WIKIDATA_PIDS = ("P580", "P582", "P585")


def wikidata_time_properties() -> list[Any]:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties as wd_properties

    props = [p for p in wd_properties(load_allowlist()) if p.pid in WIKIDATA_PIDS]
    if len(props) != len(WIKIDATA_PIDS):
        raise ValueError(f"{WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    return props


def load_contention_kg(
    engine: Any,
    store: Any,
    entities: Sequence[KgEntity],
    statements: Iterable[KgStatement],
    *,
    source_key: str,
    source_revision: str,
    raw_sha256s: Sequence[str],
    retrieved_at: Any,
    origin: str,
) -> dict[str, Any]:
    from reva.kg.model import register_properties

    register_properties(engine, [*wikidata_time_properties(), *kg_properties()])
    return load_kg(
        engine,
        store,
        entities,
        statements,
        source_key=source_key,
        source_revision=source_revision,
        raw_sha256s=raw_sha256s,
        retrieved_at=retrieved_at,
        origin=origin,
    )


class Kg:
    """Collects entities and statements for one dataset file (keys prefixed with the dataset key)."""

    def __init__(self, prefix: str, raw_sha256: str, file: str) -> None:
        self.prefix = prefix
        self.raw = raw_sha256
        self.file = file
        self.entities: list[KgEntity] = []
        self.statements: list[KgStatement] = []

    def entity(self, qid: str, kind: str, label: str, description: str) -> None:
        self.entities.append(
            KgEntity(qid=qid, kind=kind, labels={"en": label[:480]}, descriptions={"en": description[:480]})
        )

    def s(
        self, key: str, subject: str, prop: str, *, line: int, column: str | Sequence[str] | None = None, **kw: Any
    ) -> None:
        sel: dict[str, Any] = {"file": self.file, "line": line}
        if column:
            sel["column"] = column if isinstance(column, str) else list(column)
        self.statements.append(
            KgStatement(
                key=f"{self.prefix}:{key}", subject=subject, property=prop, raw_sha256=self.raw, selector=sel, **kw
            )
        )

    def string(
        self, key: str, subject: str, prop: str, value: str, *, line: int, column: Any = None, **kw: Any
    ) -> None:
        self.s(key, subject, prop, line=line, column=column, value={"type": "string", "value": value[:4000]}, **kw)

    def time(
        self, key: str, subject: str, prop: str, iso: str, precision: int, *, line: int, column: Any = None, **kw: Any
    ) -> None:
        self.s(
            key,
            subject,
            prop,
            line=line,
            column=column,
            value={"type": "time", "time": iso, "precision": precision},
            **kw,
        )

    def item(self, key: str, subject: str, prop: str, obj: str, *, line: int, column: Any = None, **kw: Any) -> None:
        self.s(key, subject, prop, line=line, column=column, object_qid=obj, **kw)

    def quantity(
        self, key: str, subject: str, prop: str, amount: str, unit: str, *, line: int, column: Any = None, **kw: Any
    ) -> None:
        val: dict[str, Any] = {"type": "quantity", "amount": amount, "unit": unit}
        bounds = kw.pop("bounds", None)
        if bounds:
            val.update(bounds)
        self.s(key, subject, prop, line=line, column=column, value=val, **kw)


# =============================================================================== NAVCO 1.2 / 1.3


@dataclass
class Navco1Row:
    line: int
    key: str
    navco_id: int
    name: str
    location: str
    target: str
    byear: int
    eyear: int
    method: str  # nonviolent | violent
    goals: list[str]
    outcome: str | None  # success | limited | failure | None
    ongoing: bool
    raw: dict[str, str] = field(repr=False, default_factory=dict)


def navco1_rows(t: Table, f: DvFile) -> tuple[list[Navco1Row], dict[str, Any]]:
    """Typed NAVCO 1.x rows with stable keys; the outcome flags must be one-hot (FAILURE may co-occur with
    ONGOING, per codebook); rows with no outcome flag are kept with ``outcome = None`` and counted."""
    seen: Counter[tuple[int, int]] = Counter()
    for _line, r in t:
        seen[(int_val(r["NAVCOID"], what="NAVCOID") or 0, int_val(r["BYEAR"], what="BYEAR") or 0)] += 1
    k_used: Counter[tuple[int, int]] = Counter()
    out: list[Navco1Row] = []
    stats: Counter[str] = Counter()
    for line, r in t:
        w = f"{f.title} line {line}"
        nid = int_val(r["NAVCOID"], what=f"{w} NAVCOID")
        by = int_val(r["BYEAR"], what=f"{w} BYEAR")
        ey = int_val(r["EYEAR"], what=f"{w} EYEAR")
        if nid is None or by is None or ey is None or not r["CAMPAIGN"] or not r["LOCATION"]:
            raise ContentionParseError(f"{w}: missing id/years/name/location", retryable=False, kind="schema_drift")
        if ey < by:
            raise ContentionParseError(f"{w}: EYEAR < BYEAR", retryable=False, kind="schema_drift")
        key = f"{nid}-{by}"
        if seen[(nid, by)] > 1:
            k_used[(nid, by)] += 1
            key = f"{key}-{k_used[(nid, by)]}"
            stats["rows_sharing_id_and_byear"] += 1
        nv, v = flag01(r["NONVIOL"], what=f"{w} NONVIOL"), flag01(r["VIOL"], what=f"{w} VIOL")
        if nv == v:
            raise ContentionParseError(f"{w}: NONVIOL/VIOL not exclusive", retryable=False, kind="schema_drift")
        flags = {k: flag01(r[k.upper()], what=f"{w} {k}") for k in OUTCOMES}
        on = [k for k in ("success", "limited", "failure") if flags[k]]
        if len(on) > 1:
            raise ContentionParseError(f"{w}: several outcome flags {on}", retryable=False, kind="schema_drift")
        if flags["ongoing"] and on != ["failure"]:
            raise ContentionParseError(f"{w}: ONGOING without FAILURE", retryable=False, kind="schema_drift")
        outcome = on[0] if on else None
        if outcome is None:
            stats["rows_without_outcome_flag"] += 1
        goals = [label for col, label in NAVCO1_GOALS.items() if flag01(r[col], what=f"{w} {col}")]
        out.append(
            Navco1Row(
                line=line,
                key=key,
                navco_id=nid,
                name=r["CAMPAIGN"],
                location=r["LOCATION"],
                target=r["TARGET"],
                byear=by,
                eyear=ey,
                method="nonviolent" if nv else "violent",
                goals=goals,
                outcome=outcome,
                ongoing=bool(flags["ongoing"]),
                raw=r,
            )
        )
    stats["rows"] = len(out)
    stats["blank_rows_skipped"] = t.blank_rows
    return out, dict(stats)


N13_CY_INDICATORS = {
    "campaigns_active": "campaigns whose [BYEAR, EYEAR] interval contains the year (EYEAR = peak year, or end year "
    "when the peak is unknown -- codebook)",
    "campaigns_active|method=nonviolent": "active campaigns coded primarily nonviolent (NONVIOL = 1)",
    "campaigns_active|method=violent": "active campaigns coded primarily violent (VIOL = 1)",
    "campaigns_active|goal=regime_change": "active campaigns with REGCHANGE = 1",
    "campaigns_active|goal=self_determination": "active campaigns with FSELFDET = 1 (independence from a foreign "
    "occupier or colonial power)",
    "campaigns_active|goal=secession": "active campaigns with SECESSION = 1",
    "campaigns_active|goal=other": "active campaigns with OTHER = 1",
    "campaign_onsets": "campaigns with BYEAR = the year",
    "campaign_onsets|method=nonviolent": "onsets of primarily nonviolent campaigns",
    "campaign_onsets|method=violent": "onsets of primarily violent campaigns",
    "campaigns_eyear": "campaigns with EYEAR (peak or end year) = the year",
    "campaigns_eyear|outcome=success": "campaigns with EYEAR = the year and SUCCESS = 1",
    "campaigns_eyear|outcome=limited": "campaigns with EYEAR = the year and LIMITED = 1",
    "campaigns_eyear|outcome=failure": "campaigns with EYEAR = the year and FAILURE = 1 (includes ongoing)",
    "campaigns_eyear|outcome=ongoing": "campaigns with EYEAR = the year and ONGOING = 1 (as of 2019-12-31)",
}
_GOAL_SLUG = {
    "regime change": "regime_change",
    "self-determination / anti-occupation": "self_determination",
    "secession": "secession",
    "other": "other",
}


def n13_atlas(rows: Sequence[Navco1Row], places: Places, *, raw_sha256: str, f: DvFile) -> AtlasRows:
    c = Counts()
    for r in rows:
        g = geo_key(places, r.location)
        for y in range(r.byear, r.eyear + 1):
            c.touch(g, y, r.location)
            for ind in ("campaigns_active", f"campaigns_active|method={r.method}"):
                c.add(g, y, ind, r.key, r.line, r.location)
            for goal in r.goals:
                c.add(g, y, f"campaigns_active|goal={_GOAL_SLUG[goal]}", r.key, r.line, r.location)
        for ind in ("campaign_onsets", f"campaign_onsets|method={r.method}"):
            c.add(g, r.byear, ind, r.key, r.line, r.location)
        c.add(g, r.eyear, "campaigns_eyear", r.key, r.line, r.location)
        if r.outcome:
            c.add(g, r.eyear, f"campaigns_eyear|outcome={r.outcome}", r.key, r.line, r.location)
        if r.ongoing:
            c.add(g, r.eyear, "campaigns_eyear|outcome=ongoing", r.key, r.line, r.location)
    return count_rows(
        c,
        source="navco",
        dataset="n13_cy",
        indicators=N13_CY_INDICATORS,
        raw_sha256=raw_sha256,
        file=f.file_name,
        method="count of NAVCO 1.3 campaign rows (keys <NAVCOID>-<BYEAR>) per location and year; locations "
        "mapped through geo scheme navco_loc, names of one country counted together",
        computed_from=f"NAVCO 1.3 campaign list ({f.revision}); 622 maximalist campaigns 1900-2019",
        extra_flags={"location_semantics": CODEBOOK["n13"]["LOCATION"], "consensus_list": True},
    )


def navco1_kg(
    rows: Sequence[Navco1Row], f: DvFile, places: Places, *, raw_sha256: str, n21_ids: frozenset[int] = frozenset()
) -> Kg:
    """Campaign events (+ NAVCO 1.2 regime violence events and links to NAVCO 2.1) and outcome entities."""
    ns = "navco12" if f.key == "n12" else "navco13"
    kg = Kg(f.key, raw_sha256, f.file_name)
    as_of = "2014" if f.key == "n12" else "2019-12-31"
    id_rows = Counter(r.navco_id for r in rows)
    for r in rows:
        ev = f"{ns}:{r.key}"
        pl = places.resolve(r.location, r.byear)
        kg.entity(
            ev,
            "event",
            f"{r.name} ({r.location}, {r.byear}-{r.eyear})",
            f"{f.title} {r.method} resistance campaign (NAVCO id {r.navco_id}); target: {r.target or 'n/a'}",
        )
        k = r.key
        kg.string(
            f"{k}:event_type",
            ev,
            "contention:event_type",
            "resistance_campaign",
            line=r.line,
            qualifiers={"dataset": f.title},
        )
        kg.string(f"{k}:provider_id", ev, "contention:provider_id", str(r.navco_id), line=r.line, column="NAVCOID")
        kg.item(
            f"{k}:location",
            ev,
            "contention:location",
            pl.node,
            line=r.line,
            column="LOCATION",
            qualifiers={"provider_location": r.location, "resolution": pl.how},
        )
        if r.target:
            kg.string(f"{k}:target", ev, "contention:target", r.target, line=r.line, column="TARGET")
        kg.time(
            f"{k}:start",
            ev,
            "P580",
            f"{r.byear:04d}-01-01",
            9,
            line=r.line,
            column="BYEAR",
            qualifiers={"definition": "Year in which campaign begins."},
        )
        kg.time(
            f"{k}:eyear",
            ev,
            "contention:peak_or_end_year",
            f"{r.eyear:04d}-01-01",
            9,
            line=r.line,
            column="EYEAR",
            qualifiers={"definition": CODEBOOK[f.key]["EYEAR"]},
        )
        kg.string(f"{k}:method", ev, "contention:primary_method", r.method, line=r.line, column=["NONVIOL", "VIOL"])
        for goal in r.goals:
            kg.string(
                f"{k}:goal:{_GOAL_SLUG[goal]}",
                ev,
                "contention:goal",
                goal,
                line=r.line,
                column=[c for c, lab in NAVCO1_GOALS.items() if lab == goal],
            )
        if r.outcome:
            value = r.outcome + (" (ongoing)" if r.ongoing else "")
            kg.string(
                f"{k}:outcome",
                ev,
                "contention:coded_outcome",
                value,
                line=r.line,
                column=["SUCCESS", "LIMITED", "FAILURE", "ONGOING"],
                qualifiers={"as_of": as_of, "definition": CODEBOOK[f.key][r.outcome.upper()]},
            )
            oc = f"{ev}:outcome"
            kg.entity(
                oc, "campaign_outcome", f"Outcome of {r.name} ({f.title}): {value}", f"coded outcome as of {as_of}"
            )
            kg.string(f"{k}:outcome:type", oc, "contention:event_type", "coded_outcome", line=r.line)
            kg.string(
                f"{k}:outcome:value",
                oc,
                "contention:coded_outcome",
                value,
                line=r.line,
                column=["SUCCESS", "LIMITED", "FAILURE", "ONGOING"],
                qualifiers={"as_of": as_of},
            )
            kg.item(f"{k}:outcome:of", oc, "contention:outcome_of", ev, line=r.line)
            kg.time(
                f"{k}:outcome:start",
                oc,
                "P585",
                f"{r.eyear:04d}-01-01",
                9,
                line=r.line,
                column="EYEAR",
                qualifiers={"definition": "codebook: outcome within a year of the peak of activities (EYEAR)"},
            )
        if f.key == "n12":
            raw = r.raw
            part = dec_text(raw.get("PARTICIPATION"))
            if part is not None:
                kg.quantity(
                    f"{k}:participation",
                    ev,
                    "contention:peak_participation",
                    part,
                    "persons",
                    line=r.line,
                    column="PARTICIPATION",
                    qualifiers={"definition": CODEBOOK["n12"]["PARTICIPATION"]},
                )
            for col, prop in (
                ("DEFECT", "contention:security_force_defection"),
                ("VIOLENTFLANK", "contention:violent_flank"),
                ("VIOLSIM", "contention:simultaneous_violent_campaign"),
                ("REGAID", "contention:external_support_regime"),
                ("STATESUP", "contention:external_support_campaign"),
                ("REGVIOL", "contention:regime_violence"),
            ):
                v = flag01(raw.get(col), what=f"n12 line {r.line} {col}")
                if v is not None:
                    kg.string(
                        f"{k}:{col.lower()}",
                        ev,
                        prop,
                        "yes" if v else "no",
                        line=r.line,
                        column=col,
                        qualifiers={"coded": "1" if v else "0"},
                    )
            if flag01(raw.get("REGVIOL"), what="REGVIOL"):
                rv = f"{ev}:regime_violence"
                kg.entity(
                    rv,
                    "event",
                    f"Regime violence against {r.name} ({r.location}, {r.byear}-{r.eyear})",
                    "state repression event as coded by NAVCO 1.2 (REGVIOL = 1); dated by the campaign period",
                )
                kg.string(
                    f"{k}:rv:type", rv, "contention:event_type", "state_repression", line=r.line, column="REGVIOL"
                )
                kg.item(
                    f"{k}:rv:directed_at",
                    rv,
                    "contention:directed_at",
                    ev,
                    line=r.line,
                    column="REGVIOL",
                    qualifiers={"definition": CODEBOOK["n12"]["REGVIOL"]},
                )
                kg.item(f"{k}:rv:location", rv, "contention:location", pl.node, line=r.line, column="LOCATION")
                kg.time(
                    f"{k}:rv:start",
                    rv,
                    "P580",
                    f"{r.byear:04d}-01-01",
                    9,
                    line=r.line,
                    column="BYEAR",
                    qualifiers={"dating": "campaign period (NAVCO 1.2 codes no date for the repression)"},
                )
            if r.navco_id in n21_ids:
                target = f"navco21:{r.navco_id}"
                if id_rows[r.navco_id] > 1:
                    kg.item(
                        f"{k}:phase_of",
                        ev,
                        "contention:phase_of",
                        target,
                        line=r.line,
                        column="NAVCOID",
                        qualifiers={"evidence": CODEBOOK["n12"]["NAVCOID"]},
                    )
                else:
                    kg.item(
                        f"{k}:same_as_n21",
                        ev,
                        "contention:same_campaign_as",
                        target,
                        line=r.line,
                        column="NAVCOID",
                        qualifiers={
                            "evidence": "NAVCO 1.2 codebook: NAVCO ID source = NAVCO 2.1 ('ID assigned to campaign.')"
                        },
                    )
    kg.entities.extend(places.entities())
    return kg


def navco1_claims(rows: Sequence[Navco1Row], f: DvFile, *, raw_sha256: str, source_key: str) -> list[dict[str, Any]]:
    ns = "navco12" if f.key == "n12" else "navco13"
    out = []
    for r in rows:
        ev = f"{ns}:{r.key}"
        sel = {"file": f.file_name, "line": r.line}
        if r.outcome:
            value = r.outcome + (" (ongoing)" if r.ongoing else "")
            var = r.outcome.upper()
            out.append(
                claim(
                    key=f"{source_key}:{f.key}:{r.key}:coded_outcome",
                    cause=ev,
                    effect=f"{ev}:outcome",
                    relation="coded_outcome",
                    f=f,
                    source_key=source_key,
                    raw_sha256=raw_sha256,
                    selector={**sel, "columns": ["SUCCESS", "LIMITED", "FAILURE", "ONGOING"]},
                    coded_value=value,
                    codebook_term="ONGOING" if r.ongoing else var,
                    scope={"place": r.location},
                )
            )
        if f.key == "n12" and flag01(r.raw.get("REGVIOL"), what="REGVIOL"):
            out.append(
                claim(
                    key=f"{source_key}:{f.key}:{r.key}:regime_violence:reaction_to",
                    cause=ev,
                    effect=f"{ev}:regime_violence",
                    relation="reaction_to",
                    f=f,
                    source_key=source_key,
                    raw_sha256=raw_sha256,
                    selector={**sel, "column": "REGVIOL"},
                    coded_value="REGVIOL = 1",
                    codebook_term="REGVIOL",
                    scope={"place": r.location},
                )
            )
    return out


# =============================================================================== NAVCO 2.1


@dataclass
class N21Year:
    line: int
    year: int
    cyear: str | None
    prim_meth: str | None
    goal: str | None
    size: str | None
    total_part: str | None
    repression: str | None
    fat_low: str | None
    fat_high: str | None
    sec_defect: bool | None
    success: bool | None
    progress: str | None


@dataclass
class N21Campaign:
    cid: int
    name: str
    location: str
    target: str
    start: date | None
    end: date | None
    status: str | None
    first_line: int
    years: list[N21Year] = field(default_factory=list)


def n21_campaigns(t: Table, f: DvFile) -> dict[int, N21Campaign]:
    out: dict[int, N21Campaign] = {}
    for line, r in t:
        w = f"{f.title} line {line}"
        cid = int_val(r["id"], what=f"{w} id")
        year = int_val(r["year"], what=f"{w} year")
        if cid is None or year is None:
            raise ContentionParseError(f"{w}: missing id/year", retryable=False, kind="schema_drift")
        c = out.get(cid)
        if c is None:
            c = out[cid] = N21Campaign(
                cid,
                r["camp_name"],
                r["location"],
                r["target"],
                parse_ymd(r["start_date"]),
                parse_ymd(r["end_date"]),
                code_str(r["status"], what=f"{w} status"),
                line,
            )
        elif c.name != r["camp_name"] or c.location != r["location"]:
            raise ContentionParseError(
                f"{w}: campaign {cid} changes name/location", retryable=False, kind="schema_drift"
            )
        pm = code_str(r["prim_meth"], what=f"{w} prim_meth")
        c.years.append(
            N21Year(
                line=line,
                year=year,
                cyear=code_str(r["cyear"], what=f"{w} cyear"),
                prim_meth={"0": "violent", "1": "nonviolent"}.get(pm or ""),
                goal=code_str(r["camp_goals"], what=f"{w} camp_goals"),
                size=code_str(r["camp_size"], what=f"{w} camp_size"),
                total_part=dec_text(r["total_part"]),
                repression=code_str(r["repression"], what=f"{w} repression"),
                fat_low=dec_text(r["fatalities_low"]),
                fat_high=dec_text(r["fatalities_high"]),
                sec_defect=flag01(r["sec_defect"], what=f"{w} sec_defect"),
                success=flag01(r["success"], what=f"{w} success"),
                progress=code_str(r["progress"], what=f"{w} progress"),
            )
        )
    for c in out.values():
        c.years.sort(key=lambda y: y.year)
        if len({y.year for y in c.years}) != len(c.years):
            raise ContentionParseError(
                f"{f.title}: campaign {c.cid} repeats a year", retryable=False, kind="schema_drift"
            )
        for y in c.years:
            if y.repression is not None and y.repression not in N21_REPRESSION:
                raise ContentionParseError(
                    f"{f.title}: repression {y.repression!r}", retryable=False, kind="schema_drift"
                )
            if y.progress is not None and y.progress not in N21_PROGRESS:
                raise ContentionParseError(f"{f.title}: progress {y.progress!r}", retryable=False, kind="schema_drift")
    return out


N21_CY_INDICATORS: dict[str, str] = {
    "campaign_years": "campaign-year rows of the location in the year",
    "campaign_years|prim_method=nonviolent": "campaign-years coded primarily nonviolent in that year (prim_meth = 1)",
    "campaign_years|prim_method=violent": "campaign-years coded primarily violent in that year (prim_meth = 0)",
    **{
        f"campaign_years|repression={v}": f"campaign-years with repression = {k} ({v})"
        for k, v in N21_REPRESSION.items()
    },
    "campaign_years|repression=unknown": "campaign-years with repression = -99 (unknown)",
    **{f"campaign_years|progress={k}": f"campaign-years with progress = {k} ({v})" for k, v in N21_PROGRESS.items()},
    **{f"campaign_years|goal={k}": f"campaign-years with camp_goals = {k} ({v})" for k, v in N21_GOALS.items()},
    "campaign_years|sec_defect=1": "campaign-years with a major security-force defection (sec_defect = 1)",
    "campaign_years|success=1": "campaign-years with success = 1 (maximalist goal achieved as a direct result of the campaign)",
}


def n21_atlas(camps: Mapping[int, N21Campaign], places: Places, *, raw_sha256: str, f: DvFile) -> AtlasRows:
    c = Counts()
    for camp in camps.values():
        g = geo_key(places, camp.location)
        for y in camp.years:
            item = f"{camp.cid}"
            c.touch(g, y.year, camp.location)
            inds = ["campaign_years"]
            if y.prim_meth:
                inds.append(f"campaign_years|prim_method={y.prim_meth}")
            inds.append(f"campaign_years|repression={N21_REPRESSION[y.repression] if y.repression else 'unknown'}")
            if y.progress:
                inds.append(f"campaign_years|progress={y.progress}")
            if y.goal in N21_GOALS:
                inds.append(f"campaign_years|goal={y.goal}")
            if y.sec_defect:
                inds.append("campaign_years|sec_defect=1")
            if y.success:
                inds.append("campaign_years|success=1")
            for ind in inds:
                c.add(g, y.year, ind, item, y.line, camp.location)
    return count_rows(
        c,
        source="navco",
        dataset="n21_cy",
        indicators=N21_CY_INDICATORS,
        raw_sha256=raw_sha256,
        file=f.file_name,
        method="count of NAVCO 2.1 campaign-year rows (items = campaign ids) per location and year; locations mapped "
        "through geo scheme navco_loc",
        computed_from=f"NAVCO 2.1 campaign-year data ({f.revision}); 384 campaigns, 2,717 campaign-years 1945-2013",
        extra_flags={
            "codebook": {"repression": CODEBOOK["n21"]["repression"], "progress": CODEBOOK["n21"]["progress"]},
            "consensus_list": True,
        },
    )


def n21_kg(camps: Mapping[int, N21Campaign], f: DvFile, places: Places, *, raw_sha256: str) -> Kg:
    kg = Kg("n21", raw_sha256, f.file_name)
    for camp in sorted(camps.values(), key=lambda c: c.cid):
        ev = f"navco21:{camp.cid}"
        y0, y1 = camp.years[0].year, camp.years[-1].year
        pl = places.resolve(camp.location, y0)
        line = camp.first_line
        k = str(camp.cid)
        kg.entity(
            ev,
            "event",
            f"{camp.name} ({camp.location}, {y0}-{y1})",
            f"NAVCO 2.1 resistance campaign {camp.cid}; target: {camp.target or 'n/a'}",
        )
        kg.string(
            f"{k}:event_type",
            ev,
            "contention:event_type",
            "resistance_campaign",
            line=line,
            qualifiers={"dataset": f.title},
        )
        kg.string(f"{k}:provider_id", ev, "contention:provider_id", str(camp.cid), line=line, column="id")
        kg.item(
            f"{k}:location",
            ev,
            "contention:location",
            pl.node,
            line=line,
            column="location",
            qualifiers={"provider_location": camp.location, "resolution": pl.how},
        )
        if camp.target:
            kg.string(f"{k}:target", ev, "contention:target", camp.target, line=line, column="target")
        if camp.start:
            kg.time(
                f"{k}:start",
                ev,
                "P580",
                camp.start.isoformat(),
                11,
                line=line,
                column="start_date",
                qualifiers={"definition": CODEBOOK["n21"]["start_date"]},
            )
        if camp.end:
            q: dict[str, Any] = {"definition": CODEBOOK["n21"]["end_date"]}
            if camp.status == "1":
                q["censored"] = "status = 1: campaign ongoing after the end of the coding period"
            kg.time(f"{k}:end", ev, "P582", camp.end.isoformat(), 11, line=line, column="end_date", qualifiers=q)
        for y in camp.years:
            yk = f"{k}:{y.year}"
            when = {
                "start": f"{y.year:04d}-01-01",
                "start_precision": 9,
                "end": f"{y.year:04d}-12-31",
                "end_precision": 9,
            }
            if y.prim_meth:
                kg.string(
                    f"{yk}:method",
                    ev,
                    "contention:primary_method",
                    y.prim_meth,
                    line=y.line,
                    column="prim_meth",
                    **when,
                )
            if y.goal in N21_GOALS:
                kg.string(
                    f"{yk}:goal", ev, "contention:goal", N21_GOALS[y.goal], line=y.line, column="camp_goals", **when
                )
            if y.size in N21_SIZE:
                kg.string(
                    f"{yk}:size",
                    ev,
                    "contention:size_category",
                    N21_SIZE[y.size],
                    line=y.line,
                    column="camp_size",
                    **when,
                )
            if y.total_part is not None:
                kg.quantity(
                    f"{yk}:participants",
                    ev,
                    "contention:peak_participation",
                    y.total_part,
                    "persons",
                    line=y.line,
                    column="total_part",
                    **when,
                )
            if y.progress:
                kg.string(
                    f"{yk}:progress",
                    ev,
                    "contention:progress",
                    N21_PROGRESS[y.progress],
                    line=y.line,
                    column="progress",
                    qualifiers={"coded": y.progress, "campaign_year_status": N21_CYEAR.get(y.cyear or "", y.cyear)},
                    **when,
                )
            if y.sec_defect is not None:
                kg.string(
                    f"{yk}:sec_defect",
                    ev,
                    "contention:security_force_defection",
                    "yes" if y.sec_defect else "no",
                    line=y.line,
                    column="sec_defect",
                    **when,
                )
            if y.repression is not None:
                kg.string(
                    f"{yk}:repression",
                    ev,
                    "contention:repression_level",
                    N21_REPRESSION[y.repression],
                    line=y.line,
                    column="repression",
                    qualifiers={"coded": y.repression},
                    **when,
                )
            if y.repression in ("1", "2", "3"):
                rp = f"{ev}:repression:{y.year}"
                kg.entity(
                    rp,
                    "event",
                    f"State repression of {camp.name} ({camp.location}, {y.year}): {N21_REPRESSION[y.repression]}",
                    "most repressive state episode in the campaign-year as coded by NAVCO 2.1",
                )
                kg.string(f"{yk}:rp:type", rp, "contention:event_type", "state_repression", line=y.line)
                kg.string(
                    f"{yk}:rp:level",
                    rp,
                    "contention:repression_level",
                    N21_REPRESSION[y.repression],
                    line=y.line,
                    column="repression",
                    qualifiers={"coded": y.repression, "definition": N21_REPRESSION_DEF[y.repression]},
                )
                kg.item(
                    f"{yk}:rp:directed_at",
                    rp,
                    "contention:directed_at",
                    ev,
                    line=y.line,
                    column="repression",
                    qualifiers={"definition": CODEBOOK["n21"]["repression"]},
                )
                kg.item(f"{yk}:rp:location", rp, "contention:location", pl.node, line=y.line, column="location")
                kg.time(f"{yk}:rp:start", rp, "P580", f"{y.year:04d}-01-01", 9, line=y.line, column="year")
                kg.time(f"{yk}:rp:end", rp, "P582", f"{y.year:04d}-12-31", 9, line=y.line, column="year")
                if y.fat_low is not None or y.fat_high is not None:
                    amount = y.fat_high if y.fat_low is None else y.fat_low
                    bounds = {}
                    if y.fat_low is not None and y.fat_high is not None and y.fat_low != y.fat_high:
                        bounds = {"lowerBound": y.fat_low, "upperBound": y.fat_high}
                    kg.quantity(
                        f"{yk}:rp:fatalities",
                        rp,
                        "contention:regime_inflicted_fatalities",
                        amount or "0",
                        "deaths",
                        line=y.line,
                        column=["fatalities_low", "fatalities_high"],
                        bounds=bounds,
                        qualifiers={
                            "estimate": "low" if y.fat_low is not None else "high",
                            "definition": "regime-inflicted fatalities during the year (low/high credible estimates)",
                        },
                    )
        end_years = [y for y in camp.years if y.cyear == "2"]
        if end_years:
            ey = end_years[-1]
            value = N21_PROGRESS.get(ey.progress or "", "unknown") + (
                "; success = 1" if ey.success else "; success = 0"
            )
            oc = f"{ev}:outcome"
            kg.entity(
                oc,
                "campaign_outcome",
                f"Outcome of {camp.name} (NAVCO 2.1): {value}",
                "progress coded in the campaign's end year and the success variable",
            )
            kg.string(f"{k}:outcome:type", oc, "contention:event_type", "coded_outcome", line=ey.line)
            kg.string(
                f"{k}:outcome:value",
                oc,
                "contention:coded_outcome",
                value,
                line=ey.line,
                column=["progress", "success"],
                qualifiers={"definition": CODEBOOK["n21"]["success"]},
            )
            kg.item(f"{k}:outcome:of", oc, "contention:outcome_of", ev, line=ey.line)
            kg.time(f"{k}:outcome:start", oc, "P585", f"{ey.year:04d}-01-01", 9, line=ey.line, column="year")
            kg.string(
                f"{k}:outcome", ev, "contention:coded_outcome", value, line=ey.line, column=["progress", "success"]
            )
    kg.entities.extend(places.entities())
    return kg


def n21_claims(
    camps: Mapping[int, N21Campaign], f: DvFile, *, raw_sha256: str, source_key: str
) -> list[dict[str, Any]]:
    out = []
    for camp in sorted(camps.values(), key=lambda c: c.cid):
        ev = f"navco21:{camp.cid}"
        _y0, _y1 = camp.years[0].year, camp.years[-1].year
        for y in camp.years:
            if y.repression in ("1", "2", "3"):
                out.append(
                    claim(
                        key=f"{source_key}:n21:{camp.cid}:{y.year}:repression:reaction_to",
                        cause=ev,
                        effect=f"{ev}:repression:{y.year}",
                        relation="reaction_to",
                        f=f,
                        source_key=source_key,
                        raw_sha256=raw_sha256,
                        selector={"file": f.file_name, "line": y.line, "column": "repression"},
                        coded_value=f"repression = {y.repression} ({N21_REPRESSION[y.repression]})",
                        codebook_term="repression",
                        scope={"place": camp.location, "period": str(y.year)},
                    )
                )
        end_years = [y for y in camp.years if y.cyear == "2"]
        if end_years:
            ey = end_years[-1]
            value = N21_PROGRESS.get(ey.progress or "", "unknown") + (
                "; success = 1" if ey.success else "; success = 0"
            )
            out.append(
                claim(
                    key=f"{source_key}:n21:{camp.cid}:coded_outcome",
                    cause=ev,
                    effect=f"{ev}:outcome",
                    relation="coded_outcome",
                    f=f,
                    source_key=source_key,
                    raw_sha256=raw_sha256,
                    selector={"file": f.file_name, "line": ey.line, "columns": ["progress", "success"]},
                    coded_value=value,
                    codebook_term="success",
                    scope={"place": camp.location},
                )
            )
    return out


# =============================================================================== MEC


@dataclass
class MecRow:
    line: int
    mid: int
    name: str
    location: str
    ccode: str
    category: str
    begin: date
    end: date
    byear: int
    eyear: int
    actor: str
    target: str
    tactics: str
    outcome: str | None
    ongoing: bool
    goals: list[tuple[str, str | None]]
    numbers: str | None
    regviol: bool | None
    raw: dict[str, str] = field(repr=False, default_factory=dict)


def mec_rows(t: Table, f: DvFile) -> tuple[list[MecRow], dict[str, Any]]:
    out = []
    stats: Counter[str] = Counter()
    for line, r in t:
        w = f"{f.title} line {line}"
        mid = int_val(r["id"], what=f"{w} id")
        b, e = parse_ymd(r["bdate"]), parse_ymd(r["edate"])
        by, ey = int_val(r["byear"], what=f"{w} byear"), int_val(r["eyear"], what=f"{w} eyear")
        cat = code_str(r["category"], what=f"{w} category")
        if mid is None or b is None or e is None or by is None or ey is None or cat not in MEC_CATEGORY:
            raise ContentionParseError(f"{w}: missing id/dates/category", retryable=False, kind="schema_drift")
        if e < b:
            raise ContentionParseError(f"{w}: edate < bdate", retryable=False, kind="schema_drift")
        unknown = flag01(r["unknown"], what=f"{w} unknown")
        flags = {k: flag01(r[k], what=f"{w} {k}") for k in OUTCOMES}
        on = [k for k in ("success", "limited", "failure") if flags[k]]
        if unknown:
            if on:
                raise ContentionParseError(f"{w}: UNKNOWN with an outcome", retryable=False, kind="schema_drift")
            stats["outcome_unknown"] += 1
            outcome = None
        else:
            if len(on) != 1 or (flags["ongoing"] and on != ["failure"]):
                raise ContentionParseError(f"{w}: outcome flags {flags}", retryable=False, kind="schema_drift")
            outcome = on[0]
        goals = []
        for col, label in MEC_GOALS.items():
            if flag01(r[col], what=f"{w} {col}"):
                desc = r.get(f"{col}description")
                desc = desc if desc and desc not in ("NA", "0") else None
                goals.append((label, desc))
        out.append(
            MecRow(
                line=line,
                mid=mid,
                name=r["campaign"],
                location=r["location"],
                ccode=r["ccode"],
                category=MEC_CATEGORY[cat],
                begin=b,
                end=e,
                byear=by,
                eyear=ey,
                actor=r["actor"],
                target=r["target"],
                tactics=r["tactics"],
                outcome=outcome,
                ongoing=bool(flags["ongoing"]),
                goals=goals,
                numbers=dec_text(r["numbers"]),
                regviol=flag01(r["regviol"], what=f"{w} regviol"),
                raw=r,
            )
        )
    if len({r.mid for r in out}) != len(out):
        raise ContentionParseError(f"{f.title}: duplicate ids", retryable=False, kind="schema_drift")
    stats["rows"] = len(out)
    return out, dict(stats)


_MEC_TACTICS = {
    "NON-VIOLENT": "nonviolent",
    "NV WITH VIOLENT FLANK": "nonviolent_with_violent_flank",
    "VIOLENT": "violent",
}

MEC_CY_INDICATORS: dict[str, str] = {
    "episodes_active": "episodes whose [bdate, edate] interval overlaps the year",
    "episodes_active|category=reformist": "active category-2 (reformist) episodes",
    "episodes_active|category=maximalist": "active category-4 (maximalist) episodes",
    **{f"episodes_active|tactics={v}": f"active episodes with tactics = {k}" for k, v in _MEC_TACTICS.items()},
    "episodes_active|regviol=1": "active episodes with regviol = 1 (state violence toward the campaign documented)",
    "episode_onsets": "episodes with bdate in the year",
    "episode_onsets|category=reformist": "reformist episodes with bdate in the year",
    "episode_onsets|category=maximalist": "maximalist episodes with bdate in the year",
    "episodes_ending": "episodes with edate in the year",
    **{f"episodes_ending|outcome={o}": f"episodes with edate in the year coded {o.upper()} = 1" for o in OUTCOMES},
    "episodes_ending|outcome=unknown": "episodes with edate in the year coded UNKNOWN = 1",
}


def mec_atlas(rows: Sequence[MecRow], places: Places, *, raw_sha256: str, f: DvFile) -> AtlasRows:
    c = Counts()
    for r in rows:
        g = geo_key(places, r.location)
        item = str(r.mid)
        for y in range(r.begin.year, r.end.year + 1):
            c.touch(g, y, r.location)
            inds = ["episodes_active", f"episodes_active|category={r.category}"]
            if r.tactics in _MEC_TACTICS:
                inds.append(f"episodes_active|tactics={_MEC_TACTICS[r.tactics]}")
            if r.regviol:
                inds.append("episodes_active|regviol=1")
            for ind in inds:
                c.add(g, y, ind, item, r.line, r.location)
        for ind in ("episode_onsets", f"episode_onsets|category={r.category}"):
            c.add(g, r.begin.year, ind, item, r.line, r.location)
        c.add(g, r.end.year, "episodes_ending", item, r.line, r.location)
        c.add(g, r.end.year, f"episodes_ending|outcome={r.outcome or 'unknown'}", item, r.line, r.location)
        if r.ongoing:
            c.add(g, r.end.year, "episodes_ending|outcome=ongoing", item, r.line, r.location)
    return count_rows(
        c,
        source="mec",
        dataset="episodes_cy",
        indicators=MEC_CY_INDICATORS,
        raw_sha256=raw_sha256,
        file=f.file_name,
        method="count of MEC episodes (items = MEC ids) per location and year; locations mapped through geo scheme "
        "mec_loc, names of one country counted together",
        computed_from=f"Major Episodes of Contention v3.0 ({f.revision}); 2,734 episodes 1955-2018",
        extra_flags={"ongoing_as_of": "2018-12-31 (edate of ongoing episodes)"},
    )


def mec_kg(rows: Sequence[MecRow], f: DvFile, places: Places, *, raw_sha256: str) -> Kg:
    kg = Kg("mec", raw_sha256, f.file_name)
    for r in rows:
        ev = f"mec:{r.mid}"
        k = str(r.mid)
        pl = places.resolve(r.location, r.begin.year)
        kg.entity(
            ev,
            "event",
            f"{r.name} ({r.location}, {r.begin.year}-{r.end.year})",
            f"MEC v3.0 {r.category} contentious episode {r.mid}; target: {r.target or 'n/a'}",
        )
        kg.string(
            f"{k}:event_type",
            ev,
            "contention:event_type",
            "contentious_episode",
            line=r.line,
            qualifiers={"dataset": f.title},
        )
        kg.string(f"{k}:provider_id", ev, "contention:provider_id", k, line=r.line, column="id")
        kg.string(f"{k}:category", ev, "contention:category", r.category, line=r.line, column="category")
        kg.item(
            f"{k}:location",
            ev,
            "contention:location",
            pl.node,
            line=r.line,
            column="location",
            qualifiers={"provider_location": r.location, "provider_ccode": r.ccode, "resolution": pl.how},
        )
        if r.actor:
            kg.string(f"{k}:actor", ev, "contention:actor", r.actor, line=r.line, column="actor")
        if r.target:
            kg.string(f"{k}:target", ev, "contention:target", r.target, line=r.line, column="target")
        kg.time(f"{k}:start", ev, "P580", r.begin.isoformat(), 11, line=r.line, column="bdate")
        kg.time(
            f"{k}:end",
            ev,
            "P582",
            r.end.isoformat(),
            11,
            line=r.line,
            column="edate",
            qualifiers={"ongoing_as_of_2018_12_31": True} if r.ongoing else {},
        )
        if r.tactics in _MEC_TACTICS:
            kg.string(
                f"{k}:method", ev, "contention:primary_method", _MEC_TACTICS[r.tactics], line=r.line, column="tactics"
            )
        for label, desc in r.goals:
            q = {"description": desc} if desc else {}
            kg.string(
                f"{k}:goal:{slug(label)}",
                ev,
                "contention:goal",
                label,
                line=r.line,
                column=[c for c, lab in MEC_GOALS.items() if lab == label],
                qualifiers=q,
            )
        if r.numbers is not None:
            kg.quantity(
                f"{k}:participants",
                ev,
                "contention:peak_participation",
                r.numbers,
                "persons",
                line=r.line,
                column="numbers",
                qualifiers={
                    "definition": "Number of members involved in peak event (conservative coding of vague reports)"
                },
            )
        for col, prop in (
            ("defect", "contention:security_force_defection"),
            ("radflank", "contention:violent_flank"),
            ("violsim", "contention:simultaneous_violent_campaign"),
            ("regaid", "contention:external_support_regime"),
            ("statesup", "contention:external_support_campaign"),
            ("regviol", "contention:regime_violence"),
        ):
            v = flag01(r.raw.get(col), what=f"mec line {r.line} {col}")
            if v is not None:
                kg.string(
                    f"{k}:{col}",
                    ev,
                    prop,
                    "yes" if v else "no",
                    line=r.line,
                    column=col,
                    qualifiers={"coded": "1" if v else "0"},
                )
        if r.outcome:
            value = r.outcome + (" (ongoing)" if r.ongoing else "")
            kg.string(
                f"{k}:outcome",
                ev,
                "contention:coded_outcome",
                value,
                line=r.line,
                column=["success", "limited", "failure", "ongoing"],
                qualifiers={"definition": CODEBOOK["mec"][r.outcome.upper()]},
            )
            oc = f"{ev}:outcome"
            kg.entity(oc, "campaign_outcome", f"Outcome of {r.name} (MEC): {value}", "coded outcome (MEC v3.0)")
            kg.string(f"{k}:outcome:type", oc, "contention:event_type", "coded_outcome", line=r.line)
            kg.string(
                f"{k}:outcome:value",
                oc,
                "contention:coded_outcome",
                value,
                line=r.line,
                column=["success", "limited", "failure", "ongoing"],
            )
            kg.item(f"{k}:outcome:of", oc, "contention:outcome_of", ev, line=r.line)
            kg.time(
                f"{k}:outcome:start",
                oc,
                "P585",
                f"{r.eyear:04d}-01-01",
                9,
                line=r.line,
                column="eyear",
                qualifiers={
                    "definition": "codebook: outcome within a year of the peak of activities (EYEAR = year of maximum membership / outcome / end)"
                },
            )
        if r.regviol:
            rv = f"{ev}:regime_violence"
            kg.entity(
                rv,
                "event",
                f"Regime violence against {r.name} ({r.location}, {r.begin.year}-{r.end.year})",
                "state repression event as coded by MEC v3.0 (regviol = 1); dated by the episode period",
            )
            kg.string(f"{k}:rv:type", rv, "contention:event_type", "state_repression", line=r.line, column="regviol")
            kg.item(
                f"{k}:rv:directed_at",
                rv,
                "contention:directed_at",
                ev,
                line=r.line,
                column="regviol",
                qualifiers={"definition": CODEBOOK["mec"]["REGVIOL"]},
            )
            kg.item(f"{k}:rv:location", rv, "contention:location", pl.node, line=r.line, column="location")
            kg.time(
                f"{k}:rv:start",
                rv,
                "P580",
                r.begin.isoformat(),
                11,
                line=r.line,
                column="bdate",
                qualifiers={"dating": "episode period (MEC codes no date for the repression)"},
            )
            kg.time(f"{k}:rv:end", rv, "P582", r.end.isoformat(), 11, line=r.line, column="edate")
    kg.entities.extend(places.entities())
    return kg


def mec_claims(rows: Sequence[MecRow], f: DvFile, *, raw_sha256: str, source_key: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        ev = f"mec:{r.mid}"
        sel = {"file": f.file_name, "line": r.line}
        if r.outcome:
            var = r.outcome.upper()
            out.append(
                claim(
                    key=f"{source_key}:mec:{r.mid}:coded_outcome",
                    cause=ev,
                    effect=f"{ev}:outcome",
                    relation="coded_outcome",
                    f=f,
                    source_key=source_key,
                    raw_sha256=raw_sha256,
                    selector={**sel, "columns": ["success", "limited", "failure", "ongoing", "unknown"]},
                    coded_value=r.outcome + (" (ongoing)" if r.ongoing else ""),
                    codebook_term="ONGOING" if r.ongoing else var,
                    scope={"place": r.location, "category": r.category},
                )
            )
        if r.regviol:
            out.append(
                claim(
                    key=f"{source_key}:mec:{r.mid}:regime_violence:reaction_to",
                    cause=ev,
                    effect=f"{ev}:regime_violence",
                    relation="reaction_to",
                    f=f,
                    source_key=source_key,
                    raw_sha256=raw_sha256,
                    selector={**sel, "column": "regviol"},
                    coded_value="regviol = 1",
                    codebook_term="REGVIOL",
                    scope={"place": r.location, "category": r.category},
                )
            )
    return out


# =============================================================================== NAVCO 3.0 (counts only)

XLSX_MEDIA = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream")
#: NAVCO 3.0 codebook (Lewis, Chenoweth & Pinckney, rev. 2016-07-28), quoted verbatim.
N30_CODEBOOK = {
    "unit": "BASIC EVENT CODING SRUCTURE: LTG + ACTOR + VERB + TARGET + AUX",
    "lines": "Single events in sources may in fact constitute multiple actions according to the 21-verb ontology.",
    "reciprocal": "In these cases, separate lines indicating both sides of the reciprocal event should be coded with "
    "the same verb.",
    "date": "The date variable should always reflect a single day, not a range of days.",
    "actor_id": "1 = State 2 = Non-state 3 = International 4=nonaligned 5=local state actors",
    "tactic_choice": "0=primarily violent event; 1=primarily nonviolent event; 2=mixed nonviolent and violent activity",
    "st_posture": "The degree of conciliation or repression embedded in the regime's actions in reaction to campaign "
    "activity.",
}
N30_ACTOR = {"1": "state", "2": "non_state", "3": "international", "4": "nonaligned", "5": "local_state"}
N30_TACTIC = {"0": "violent", "1": "nonviolent", "2": "mixed"}
N30_POSTURE = {
    "1": "full accommodation",
    "2": "material concessions",
    "3": "non-material concessions",
    "4": "neutral",
    "5": "non-material and non-physical repression",
    "6": "material and/or physical repression short of killing",
    "7": "material and/or physical repression intended to result in death",
}
#: Section 2 verb codelist, root codes (NAVCO 3.0's modified CAMEO ontology, 21 verbs).
N30_VERBS = {
    1: "MAKE PUBLIC STATEMENT",
    2: "APPEAL",
    3: "EXPRESS INTENT TO COOPERATE",
    4: "CONSULT",
    5: "ENGAGE IN POLITICAL COOPERATION",
    6: "ENGAGE IN MATERIAL COOPERATION",
    7: "PROVIDE AID",
    8: "YIELD",
    9: "INVESTIGATE",
    10: "DEMAND",
    11: "DISAPPROVE",
    12: "REJECT",
    13: "THREATEN",
    14: "PROTEST",
    15: "EXHIBIT FORCE POSTURE",
    16: "REDUCE RELATIONS",
    17: "COERCE",
    18: "REPRESS AND ABUSE",
    19: "ENGAGE IN VIOLENT COMBAT",
    20: "USE UNCONVENTIONAL MASS VIOLENCE",
    21: "DEFECT",
}
EXCEL_EPOCH = date(1899, 12, 30)  # 1900 date system (the workbook declares no date1904)


def n30_verb_slug(code: int) -> str:
    return f"{code:02d}_{slug(N30_VERBS[code])}"


@dataclass
class N30Line:
    row: int
    country: str
    cowcode: str
    day: date
    actor: str | None
    verb: int | None
    tactic: str | None
    posture: str | None


def _xtext(cells: Mapping[int, Any], i: int) -> str:
    c = cells.get(i)
    return c.text.strip() if c is not None and c.text is not None else ""


def n30_lines(body: bytes, f: DvFile, *, strict: bool = True) -> tuple[list[N30Line], dict[str, Any]]:
    """Coded action lines of the NAVCO 3.0 workbook (only the coded columns are read: the news lead, source
    titles, localities, actor names and notes are never parsed into REVA)."""
    from datetime import timedelta

    from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError

    try:
        wb = Workbook(body)
        if b'date1904="1"' in wb._read("xl/workbook.xml"):  # the 1904 date system would shift every date
            raise ContentionParseError(f"{f.title}: 1904 date system", retryable=False, kind="schema_drift")
        sheet = next(iter(wb.sheets))
        hdr: dict[int, str] | None = None
        out: list[N30Line] = []
        stats: Counter[str] = Counter()
        for rn, cells in wb.rows(sheet):
            if hdr is None:
                hdr = {i: c.text for i, c in cells.items()}
                missing = [c for c in f.columns if c not in hdr.values()]
                if missing:
                    raise ContentionParseError(
                        f"{f.title}: missing columns {missing}", retryable=False, kind="schema_drift"
                    )
                col = {v: k for k, v in hdr.items()}
                continue
            vals = {name: _xtext(cells, col[name]) for name in f.columns}
            country, serial = vals["country_name"], vals["date"]
            if not country or not serial:
                raise ContentionParseError(
                    f"{f.title} row {rn}: missing country/date", retryable=False, kind="schema_drift"
                )
            try:
                day = EXCEL_EPOCH + timedelta(days=int(Decimal(serial)))
            except (InvalidOperation, ValueError, OverflowError) as e:
                raise ContentionParseError(
                    f"{f.title} row {rn}: date {serial!r}", retryable=False, kind="schema_drift"
                ) from e
            actor = vals["actor_id"] or None
            if actor is not None and actor not in N30_ACTOR:
                stats["actor_id_outside_codebook"] += 1
                actor = None
            verb = int_val(vals["verb_10"], what=f"{f.title} row {rn} verb_10")
            if verb is not None and verb not in N30_VERBS:
                stats["verb_outside_codebook"] += 1
                verb = None
            tactic = vals["tactical_choice"] or None
            if tactic is not None and tactic not in N30_TACTIC:
                stats["tactic_outside_codebook"] += 1
                tactic = None
            posture = vals["st_posture"] or None
            if posture is not None and posture not in N30_POSTURE:
                stats["st_posture_outside_codebook"] += 1
                posture = None
            out.append(N30Line(rn, country, vals["cowcode"], day, actor, verb, tactic, posture))
    except XlsxError as e:
        raise ContentionParseError(f"{f.title}: workbook unreadable: {e}", retryable=False, kind="schema_drift") from e
    if strict and len(out) < f.min_rows:
        raise ContentionParseError(
            f"{f.title}: {len(out)} rows < {f.min_rows} (truncated?)", retryable=False, kind="incomplete"
        )
    stats["action_lines"] = len(out)
    stats["countries"] = len({x.country for x in out})
    return out, dict(stats)


N30_CY_INDICATORS: dict[str, str] = {
    "action_lines": "coded action lines dated in the year (one actor-verb-target action per line; a news event can "
    "give several lines and reciprocal actions are coded once per side -- codebook)",
    **{
        f"action_lines|actor={s}": f"action lines with actor_id = {k} ({s.replace('_', ' ')})"
        for k, s in N30_ACTOR.items()
    },
    **{
        f"action_lines|tactic={s}": f"action lines with tactical_choice = {k} (primarily {s})"
        for k, s in N30_TACTIC.items()
    },
    **{
        f"action_lines|verb={n30_verb_slug(k)}": f"action lines with verb_10 = {k} ({lab})"
        for k, lab in N30_VERBS.items()
    },
    **{
        f"action_lines|state_posture={k}": f"action lines with st_posture = {k} ({lab})"
        for k, lab in N30_POSTURE.items()
    },
}


def n30_atlas(lines: Sequence[N30Line], places: Places, *, raw_sha256: str, f: DvFile) -> AtlasRows:
    c = Counts()
    for x in lines:
        g = geo_key(places, x.country)
        y = x.day.year
        item = str(x.row)
        c.touch(g, y, x.country)
        inds = ["action_lines"]
        if x.actor:
            inds.append(f"action_lines|actor={N30_ACTOR[x.actor]}")
        if x.tactic:
            inds.append(f"action_lines|tactic={N30_TACTIC[x.tactic]}")
        if x.verb is not None:
            inds.append(f"action_lines|verb={n30_verb_slug(x.verb)}")
        if x.posture:
            inds.append(f"action_lines|state_posture={x.posture}")
        for ind in inds:
            c.add(g, y, ind, item, x.row, x.country)
    return count_rows(
        c,
        source="navco",
        dataset="n30_cy",
        indicators=N30_CY_INDICATORS,
        raw_sha256=raw_sha256,
        file=f.file_name + "#Sheet1",
        method="count of NAVCO 3.0 action lines (items = workbook row numbers) per country and year of the coded day; "
        "countries mapped through geo scheme navco3_loc",
        computed_from=f"NAVCO 3.0 country-day action data ({f.revision}); 112,381 lines, 26 countries, 1990-2012",
        extra_flags={
            "coverage": "only the 26 NAVCO 3.0 countries, years with >= 1 coded line; counts depend on news coverage",
            "unit": N30_CODEBOOK["lines"] + " " + N30_CODEBOOK["reciprocal"],
        },
        max_items=0,
    )


# =============================================================================== sources + ingest

SOURCE_KEY_NAVCO = "harvard.dataverse.navco"
SOURCE_KEY_MEC = "harvard.dataverse.mec"
RIGHTS_NAVCO = {
    "licence": LICENCE,
    "licence_url": LICENCE_URL,
    "attribution": "Chenoweth & Shay, NAVCO 1.2 (doi:10.7910/DVN/0UZOTX v2), NAVCO 1.3 list (doi:10.7910/DVN/ON9XND v1), "
    "NAVCO 2.1 (doi:10.7910/DVN/MHOXDV v2.1); Chenoweth, Pinckney & Lewis, NAVCO 3.0 (doi:10.7910/DVN/INNYEO v1); "
    "Harvard Dataverse",
}
RIGHTS_MEC = {
    "licence": LICENCE,
    "licence_url": LICENCE_URL,
    "attribution": FILES["mec"].citation,
}


def atlas_source() -> list[Any]:
    from reva.atlas.sources import AtlasSourceSpec

    common: dict[str, Any] = dict(
        version=CONNECTOR_VERSION,
        licence=LICENCE,
        allowed_hosts=HOSTS,
        cadence_seconds=180 * 86400,
        max_response_bytes=5_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
    )
    return [
        AtlasSourceSpec(
            name="atlas.navco",
            source_key=SOURCE_KEY_NAVCO,
            atlas_source="navco",
            provider="Harvard Dataverse (Chenoweth & Shay, NAVCO data project)",
            datasets=("n13_cy", "n21_cy", "n30_cy"),
            dataset_ids=tuple(FILES[k].revision for k in ("n12", "n13", "n21", "n30")),
            planned_requests=10,
            field_mappings={
                "geo": "provider location names -> geo scheme navco_loc (registry/atlas/geo_aliases/a_contention.yaml)",
                "time": "campaign years (BYEAR..EYEAR) / campaign-years",
                "kg": "navco12:<id>-<byear>, navco13:<id>-<byear>, navco21:<id> (NAVCO 3.0: atlas counts only)",
            },
            notes="Pinned Dataverse versions (1.2 v2.0, 1.3 v1.0, 2.1 v2.1, 3.0 v1.0); .tab exports checked against the "
            "version record's filesize, the 3.0 workbook by md5; quoted codebooks checked by md5. T1 claims "
            "(coded_outcome, reaction_to) through reva.causal.load_dataset_claims.",
            **{**common, "max_response_bytes": 25_000_000},
        ),
        AtlasSourceSpec(
            name="atlas.mec",
            source_key=SOURCE_KEY_MEC,
            atlas_source="mec",
            provider="Harvard Dataverse (Chenoweth & Kang, Major Episodes of Contention)",
            datasets=("episodes_cy",),
            dataset_ids=(FILES["mec"].revision,),
            planned_requests=3,
            field_mappings={
                "geo": "provider location names -> geo scheme mec_loc",
                "time": "bdate..edate",
                "kg": "mec:<id>",
            },
            notes="Pinned Dataverse version 3.0; categories 2 = reformist, 4 = maximalist.",
            **common,
        ),
    ]


async def fetch_file(
    run: Any, f: DvFile, *, max_bytes: int = 5_000_000, media_types: tuple[str, ...] = TAB_MEDIA
) -> tuple[Any, Any]:
    meta = await run.get(
        f.version_url, key=f"{f.key}:version:{f.version}", media_types=("application/json",), max_bytes=2_000_000
    )
    data = await run.get(f.file_url, key=f"{f.key}:{f.file_id}", media_types=media_types, max_bytes=max_bytes)
    return meta, data


@dataclass
class Parsed:
    f: DvFile
    meta_raw: Any
    data_raw: Any
    sha: str
    table: Table
    meta: dict[str, Any]


async def fetch_parse(run: Any, key: str, *, strict: bool) -> Parsed:
    return await fetch_parse_file(run, FILES[key], strict=strict)


async def fetch_parse_file(run: Any, f: DvFile, *, strict: bool, max_bytes: int = 5_000_000) -> Parsed:
    """Version record + tab file through the run's client, checked and parsed (nothing archived yet)."""
    meta_raw, data_raw = await fetch_file(run, f, max_bytes=max_bytes)
    meta = check_version(meta_raw.body, data_raw.body, f)
    table = read_tab(data_raw.body, f, strict=strict)
    return Parsed(f, meta_raw, data_raw, sha256(data_raw.body), table, meta)


def build_navco(parsed: Mapping[str, Parsed], *, qid_for_iso3: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Pure: parsed NAVCO files -> atlas rows, KG records and staged claims."""
    p12, p13, p21 = parsed["n12"], parsed["n13"], parsed["n21"]
    r12, s12 = navco1_rows(p12.table, p12.f)
    r13, s13 = navco1_rows(p13.table, p13.f)
    c21 = n21_campaigns(p21.table, p21.f)
    places = Places("navco_loc", "navco", qid_for_iso3)
    kg12 = navco1_kg(r12, p12.f, places, raw_sha256=p12.sha, n21_ids=frozenset(c21))
    kg13 = navco1_kg(r13, p13.f, places, raw_sha256=p13.sha)
    kg21 = n21_kg(c21, p21.f, places, raw_sha256=p21.sha)
    ents: dict[str, KgEntity] = {}
    for kg in (kg12, kg13, kg21):
        for e in kg.entities:
            ents.setdefault(e.qid, e)
    claims = {
        "navco12": navco1_claims(r12, p12.f, raw_sha256=p12.sha, source_key=SOURCE_KEY_NAVCO),
        "navco13": navco1_claims(r13, p13.f, raw_sha256=p13.sha, source_key=SOURCE_KEY_NAVCO),
        "navco21": n21_claims(c21, p21.f, raw_sha256=p21.sha, source_key=SOURCE_KEY_NAVCO),
    }
    return {
        "n13_cy": n13_atlas(r13, places, raw_sha256=p13.sha, f=p13.f),
        "n21_cy": n21_atlas(c21, places, raw_sha256=p21.sha, f=p21.f),
        "entities": [ents[k] for k in sorted(ents)],
        "statements": kg12.statements + kg13.statements + kg21.statements,
        "claims": claims,
        "stats": {"n12": s12, "n13": s13, "n21": {"campaigns": len(c21), "campaign_years": len(p21.table)}},
        "unmapped_locations": sorted(
            {r.location for r in r12 + r13 if places.iso3(r.location) is None}
            | {c.location for c in c21.values() if places.iso3(c.location) is None}
        ),
    }


def build_mec(p: Parsed, *, qid_for_iso3: Mapping[str, str] | None = None) -> dict[str, Any]:
    rows, stats = mec_rows(p.table, p.f)
    places = Places("mec_loc", "mec", qid_for_iso3)
    kg = mec_kg(rows, p.f, places, raw_sha256=p.sha)
    return {
        "episodes_cy": mec_atlas(rows, places, raw_sha256=p.sha, f=p.f),
        "entities": kg.entities,
        "statements": kg.statements,
        "claims": {"mec": mec_claims(rows, p.f, raw_sha256=p.sha, source_key=SOURCE_KEY_MEC)},
        "stats": stats,
        "unmapped_locations": sorted({r.location for r in rows if places.iso3(r.location) is None}),
    }


#: claim dataset name -> FILES key
CLAIM_DATASETS = {"navco12": "n12", "navco13": "n13", "navco21": "n21", "mec": "mec"}


def claim_counts(claims: Mapping[str, Sequence[Any]]) -> dict[str, int]:
    return dict(sorted(Counter(c.relation for cs in claims.values() for c in cs).items()))


async def _ingest_navco(run: Any) -> dict[str, Any]:
    strict = run.origin == "live"
    parsed = {k: await fetch_parse(run, k, strict=strict) for k in ("n12", "n13", "n21")}
    out = build_navco(parsed)
    for p in parsed.values():  # parsed and checked: archive raw bytes (version records too)
        run.archive(p.meta_raw)
        run.archive(p.data_raw)
    res: dict[str, Any] = {"stats": out["stats"], "atlas": {}, "unmapped_locations": out["unmapped_locations"]}
    for ds, key, note in (
        (
            "n13_cy",
            "n13",
            "COMPUTED counts per country-year from the NAVCO 1.3 campaign list (622 campaigns, 1900-2019)",
        ),
        ("n21_cy", "n21", "COMPUTED counts per country-year from NAVCO 2.1 campaign-years (384 campaigns, 1945-2013)"),
    ):
        if run.wants(ds):
            r = run.register(
                dataset=ds,
                frame=atlas_frame(out[ds]),
                raw_sha256s=[parsed[key].sha],
                rights=RIGHTS_NAVCO,
                coverage_note=note + "; only country-years with >= 1 campaign are listed",
            )
            res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    if run.wants("n30_cy"):
        res["atlas"]["n30_cy"], res["stats"]["n30"] = await _ingest_n30(run, strict=strict)
    files = {ds: parsed[CLAIM_DATASETS[ds]] for ds in out["claims"]}
    return finish(run, res, out, files=files, source_key=SOURCE_KEY_NAVCO, revision="navco-1.2v2.0-1.3v1.0-2.1v2.1")


async def _ingest_n30(run: Any, *, strict: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """NAVCO 3.0 workbook (20.7 MB): version record + workbook, md5-checked, counted, archived, registered; the
    parsed lines are released before returning (no KG: the rows carry news leads and named actors)."""
    import gc

    f = FILES["n30"]
    meta_raw, data_raw = await fetch_file(run, f, max_bytes=25_000_000, media_types=XLSX_MEDIA)
    check_version(meta_raw.body, data_raw.body, f)
    sha = sha256(data_raw.body)
    lines, stats = n30_lines(data_raw.body, f, strict=strict)
    rows = n30_atlas(lines, Places("navco3_loc", "navco"), raw_sha256=sha, f=f)
    del lines
    gc.collect()
    run.archive(meta_raw)
    run.archive(data_raw)
    r = run.register(
        dataset="n30_cy",
        frame=atlas_frame(rows),
        raw_sha256s=[sha],
        rights=RIGHTS_NAVCO,
        coverage_note="COMPUTED counts per country-year of NAVCO 3.0 coded action lines (26 countries, 1990-2012); "
        "only country-years with >= 1 line are listed; text columns (news leads, actor names) are not read",
    )
    return {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}, stats


async def _ingest_mec(run: Any) -> dict[str, Any]:
    p = await fetch_parse(run, "mec", strict=run.origin == "live")
    out = build_mec(p)
    run.archive(p.meta_raw)
    run.archive(p.data_raw)
    res: dict[str, Any] = {"stats": out["stats"], "atlas": {}, "unmapped_locations": out["unmapped_locations"]}
    if run.wants("episodes_cy"):
        r = run.register(
            dataset="episodes_cy",
            frame=atlas_frame(out["episodes_cy"]),
            raw_sha256s=[p.sha],
            rights=RIGHTS_MEC,
            coverage_note="COMPUTED counts per country-year from MEC v3.0 (2,734 episodes 1955-2018); only country-years with >= 1 episode are listed",
        )
        res["atlas"]["episodes_cy"] = {
            k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")
        }
    return finish(run, res, out, files={"mec": p}, source_key=SOURCE_KEY_MEC, revision="mec-v3.0")


def finish(
    run: Any,
    res: dict[str, Any],
    out: Mapping[str, Any],
    *,
    files: Mapping[str, Parsed],
    source_key: str,
    revision: str,
) -> dict[str, Any]:
    """Load the KG records, then deliver the claims (one complete delivery per dataset file)."""
    parsed = list({id(p): p for p in files.values()}.values())
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    res["claims"] = claim_counts(out["claims"])
    if run.dataset is None and kg_available(run.engine):
        res["kg"] = load_contention_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=source_key,
            source_revision=revision,
            raw_sha256s=sorted({p.sha for p in parsed}),
            retrieved_at=max(p.data_raw.retrieved_at for p in parsed),
            origin=run.origin,
        )
    if run.dataset is None:
        deliveries = [
            delivery(
                files[ds].f,
                ds,
                cs,
                source_key=source_key,
                raw_sha256=files[ds].sha,
                retrieved_at=files[ds].data_raw.retrieved_at,
                origin=run.origin,
            )
            for ds, cs in out["claims"].items()
        ]
        res["claims_delivery"] = deliver(run, deliveries)
    return res


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == "atlas.navco":
        return await _ingest_navco(run)
    if run.spec.name == "atlas.mec":
        return await _ingest_mec(run)
    raise ValueError(f"unknown spec {run.spec.name}")
