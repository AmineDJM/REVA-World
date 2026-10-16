"""Colonial history of every country (agent A-COLONIAL): COLDAT and ICOW -> atlas tables + KG statements.

Sources (atlas source platform, ``docs/dev/v3_atlas_interface.md`` section 7; every request goes through ``run.get``)
--------------------------------------------------------------------------------------------------------------
* ``atlas.coldat`` -- **COLDAT, Colonial Dates Dataset** (Becker 2019, SOCIUM SFB 1342 WorkingPapers No. 2), Harvard
  Dataverse ``doi:10.7910/DVN/T9SDEW`` version 3.0 (2023-09-21), **CC0 1.0** (checked in the pinned version record on
  every run). Files: ``COLDAT_dyads.tab`` (long: one row per contemporary state x European colonizer),
  ``COLDAT_colonies.tab`` (wide: the same values, cross-checked cell by cell; any difference is reported, never
  resolved) and ``read_me.txt`` (update notes). The quoted variable definitions come from the paper's Appendix I
  (``Introducing COLDAT.pdf``, md5 pinned against the version record).
* ``atlas.icow`` -- **ICOW Colonial History Data Set 1.1** (Hensel 2018) and **ICOW Territorial Claims Data,
  provisional version 1.01** (Frederick, Hensel & Macaulay 2017), ``https://www.paulhensel.org/Data/``. Terms: free
  download, cite, *"Please do not redistribute ICOW data to other scholars"* (a professional courtesy, not an open
  licence) -- recorded in ``registry/sources/icow.yaml`` and flagged for maintainer review. The countrycode 1.9.0
  tarball (CRAN) is fetched and archived by the run: it is the provenance of the COW-code crosswalk.

What is produced (no value is computed except where a row says ``computed_from``)
---------------------------------------------------------------------------------
* atlas ``coldat.dyads_v3``: per (country, colonizer) the coded dummy ``colonized|colonizer=<c>`` and, for both
  COLDAT aggregations (``agg=max`` = "last date aggregation", ``agg=mean`` = "mean aggregation"), the colonial spell
  ``colonial_rule|...`` (value_text = colonizer, period = [first year, last year + 1)) and its bounds
  ``colonial_rule_start|...`` / ``colonial_rule_end|...`` (years as reported).
* atlas ``icow.colhist_v11``: per COW state: independence year, former ruler, violent independence (0/1), type of
  independence, primary colonial ruler, secession, merger, COW / Gleditsch-Ward system entry -- as coded.
* atlas ``icow.claims_prov101``: one row per dyadic territorial claim and claimant (challenger / target), period =
  [begin month, end month) as coded, resolution / salience / MIDs over the issue in the flags.
* KG (source keys ``harvard.dataverse.coldat`` and ``icow.paulhensel``): ``coldat:colonized_by`` (country ->
  colonizer, dated, one statement per aggregation); ICOW independence / secession / merger events
  (``icowcol:independence:<ccode>`` ...) with ``P585`` dates, former ruler, coded mode and type; the primary colonial
  ruler; ICOW dyadic claims ``icowclaim:<claimdy>`` with ``P580``/``P582``, challenger, target, resolution.
* ``colonial:independence_key`` = ``independence|cow:<state>|from|cow:<former ruler>`` on every independence event
  that names its former ruler (also emitted by ``atlas_terrchange`` on COW Territorial Change independences): a
  shared-identifier value for EVENTS' ``same_property`` rule (``events:same_as`` is EVENTS' to write).

**No causal claim.** ``registry/causal/relations_colonial.yaml`` lists every coded variable with its codebook text: the
mode of independence (``IndViol``), claim resolutions and MID counts do not identify a cause event, so they are
attributes / context, never ``kg.causal_claim`` rows (docs/dev/v3_a_colonial.md section 6).

Country codes: COLDAT names -> crosswalk through the geo scheme ``coldat_name``
(``registry/atlas/geo_aliases/a_colonial.yaml``); COW state numbers -> KGGEOPOL's date-aware ``StateCodeCrosswalk``
(``atlas_geopol_common``), historical states keep ``iso3 = null`` with their verified QID.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import (
    AtlasRows,
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    StateEntityResolver,
)
from reva.kernel.errors import SourceResponseError

CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION_COLDAT = "atlas_colonial_coldat/1"
PARSER_VERSION_ICOW = "atlas_colonial_icow/1"


class ColonialParseError(SourceResponseError):
    pass


def drift(msg: str, kind: str = "schema_drift") -> ColonialParseError:
    return ColonialParseError(msg, retryable=False, kind=kind)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def slug(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", s.casefold()).strip("_")
    return out[:100] or "x"


# =============================================================================== shared KG predicates

#: Dataset predicates of A-COLONIAL: pid -> (value kind, reva predicate, en label, fr label, inverse name, inverse en).
#: None is causal (causal links are claims, never KG predicates; there are none from these datasets).
COLONIAL_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "colonial:independence_key": ("string", "colonial_independence_key", "independence key (independent state + former ruler, COW codes)", "clé d'indépendance (État indépendant + ancien souverain, codes COW)", None, None),
    "colonial:event_type": ("string", "colonial_event_type", "event type (dataset kind)", "type d'événement (jeu de données)", None, None),
    "coldat:colonized_by": ("item", "coldat_colonized_by", "colonized by (COLDAT, European colonial power)", "colonisé par (COLDAT, puissance coloniale européenne)", "coldat_colonizer_of", "colonizer of (COLDAT)"),
    "icowcol:primary_colonial_ruler": ("item", "icow_primary_colonial_ruler", "primary colonial ruler (ICOW ColRuler)", "principal souverain colonial (ICOW ColRuler)", "icow_primary_colonial_ruler_of", "primary colonial ruler of (ICOW)"),
    "icowcol:independence_of": ("item", "icow_independence_of", "independence of (ICOW state)", "indépendance de (État ICOW)", "icow_has_independence", "independence (ICOW)"),
    "icowcol:independence_from": ("item", "icow_independence_from", "independence gained from (ICOW IndFrom)", "indépendance obtenue de (ICOW IndFrom)", "icow_lost_dependency_in", "former ruler in independence (ICOW)"),
    "icowcol:independence_violent": ("string", "icow_independence_violent", "violent independence? (ICOW IndViol, coded)", "indépendance violente ? (ICOW IndViol, codé)", None, None),
    "icowcol:independence_type": ("string", "icow_independence_type", "type of independence (ICOW IndType)", "type d'indépendance (ICOW IndType)", None, None),
    "icowcol:coder_note": ("string", "icow_coder_note", "coder note (ICOW Notes, verbatim)", "note du codeur (ICOW Notes, verbatim)", None, None),
    "icowcol:secession_of": ("item", "icow_secession_of", "secession of (ICOW state)", "sécession de (État ICOW)", "icow_has_secession", "secession (ICOW)"),
    "icowcol:seceded_from": ("item", "icow_seceded_from", "seceded from (ICOW SecFrom)", "a fait sécession de (ICOW SecFrom)", "icow_secession_from_of", "state left in secession (ICOW)"),
    "icowcol:secession_violent": ("string", "icow_secession_violent", "violent secession? (ICOW SecViol, coded)", "sécession violente ? (ICOW SecViol, codé)", None, None),
    "icowcol:merger_of": ("item", "icow_merger_of", "merger of (ICOW state)", "fusion de (État ICOW)", "icow_has_merger", "merger (ICOW)"),
    "icowcol:merged_into": ("item", "icow_merged_into", "merged into (ICOW Into)", "a fusionné dans (ICOW Into)", "icow_absorbed_in", "absorbing state in merger (ICOW)"),
    "icowcol:cow_system_entry": ("time", "icow_cow_system_entry", "COW interstate system entry (ICOW COWsys)", "entrée dans le système interétatique COW (ICOW)", None, None),
    "icowcol:gw_system_entry": ("time", "icow_gw_system_entry", "Gleditsch-Ward system entry (ICOW GWsys)", "entrée dans le système Gleditsch-Ward (ICOW)", None, None),
    "icowclaim:challenger": ("item", "icow_claim_challenger", "challenger (ICOW territorial claim)", "demandeur (revendication territoriale ICOW)", "icow_challenger_in", "challenger in territorial claim (ICOW)"),
    "icowclaim:target": ("item", "icow_claim_target", "target (ICOW territorial claim)", "cible (revendication territoriale ICOW)", "icow_target_in", "target of territorial claim (ICOW)"),
    "icowclaim:territory": ("string", "icow_claim_territory", "claimed territory (ICOW name)", "territoire revendiqué (nom ICOW)", None, None),
    "icowclaim:claim_number": ("string", "icow_claim_number", "ICOW claim number (territory case)", "numéro de revendication ICOW (cas territorial)", None, None),
    "icowclaim:region": ("string", "icow_claim_region", "region of the claimed territory (ICOW)", "région du territoire revendiqué (ICOW)", None, None),
    "icowclaim:resolution": ("string", "icow_claim_resolution", "type of claim resolution (ICOW resolved, coded)", "type de résolution (ICOW resolved, codé)", None, None),
    "icowclaim:ended_through_violence": ("string", "icow_claim_ended_through_violence", "claim ended through organized violence? (ICOW endviol, coded)", "fin par violence organisée ? (ICOW endviol, codé)", None, None),
    "icowclaim:salience": ("quantity", "icow_claim_salience", "salience index 0-12, highest while the claim was ongoing (ICOW icowsal)", "indice de saillance 0-12 (ICOW icowsal)", None, None),
    "icowclaim:mids_over_issue": ("quantity", "icow_claim_mids", "militarized disputes over the issue (ICOW midsiss, count)", "différends militarisés sur l'enjeu (ICOW midsiss)", None, None),
    "icowclaim:fatal_mids_over_issue": ("quantity", "icow_claim_fatal_mids", "fatal militarized disputes over the issue (ICOW midsfat, count)", "différends militarisés mortels sur l'enjeu (ICOW midsfat)", None, None),
    "icowclaim:wars_over_issue": ("quantity", "icow_claim_wars", "interstate wars over the issue (ICOW warsiss, count)", "guerres interétatiques sur l'enjeu (ICOW warsiss)", None, None),
}  # fmt: skip

#: Wikidata time properties (WDHARVEST's definitions): start, end, point in time -- the dates the causal grader and
#: the event view read.
WIKIDATA_PIDS = ("P580", "P582", "P585")


def kg_properties(extra: Mapping[str, tuple[str, str, str, str, str | None, str | None]] | None = None) -> list[Any]:
    from reva.kg.model import KgProperty

    props = {**COLONIAL_PROPERTIES, **dict(extra or {})}
    return [
        KgProperty(
            pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv, inverse_label_en=ile
        )
        for p, (k, rp, le, lf, inv, ile) in props.items()
    ]


def wikidata_time_properties() -> list[Any]:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties as wd_properties

    props = [p for p in wd_properties(load_allowlist()) if p.pid in WIKIDATA_PIDS]
    if len(props) != len(WIKIDATA_PIDS):
        raise ValueError(f"{WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    return props


def load_colonial_kg(
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
    extra_properties: Mapping[str, tuple[str, str, str, str, str | None, str | None]] | None = None,
) -> dict[str, Any]:
    """Register the predicates (Wikidata time PIDs + A-COLONIAL's + KGGEOPOL's), then KGGEOPOL's bounded loader."""
    from reva.adapters.connectors.atlas_geopol_common import load_kg
    from reva.kg.model import register_properties

    register_properties(engine, [*wikidata_time_properties(), *kg_properties(extra_properties)])
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
    """Entities and statements of one dataset file (statement keys prefixed by the dataset key)."""

    def __init__(self, prefix: str, raw_sha256: str, file: str) -> None:
        self.prefix = prefix
        self.raw = raw_sha256
        self.file = file
        self.entities: dict[str, KgEntity] = {}
        self.statements: list[KgStatement] = []

    def entity(self, qid: str, kind: str, label: str, description: str) -> None:
        if qid not in self.entities:
            self.entities[qid] = KgEntity(
                qid=qid, kind=kind, labels={"en": label[:480]}, descriptions={"en": description[:480]}
            )

    def s(self, key: str, subject: str, prop: str, *, sel: Mapping[str, Any], **kw: Any) -> None:
        self.statements.append(
            KgStatement(
                key=f"{self.prefix}:{key}",
                subject=subject,
                property=prop,
                raw_sha256=self.raw,
                selector={"file": self.file, **dict(sel)},
                **kw,
            )
        )

    def string(self, key: str, subject: str, prop: str, value: str, *, sel: Mapping[str, Any], **kw: Any) -> None:
        self.s(key, subject, prop, sel=sel, value={"type": "string", "value": str(value)[:4000]}, **kw)

    def time(self, key: str, subject: str, prop: str, t: CodedTime, *, sel: Mapping[str, Any], **kw: Any) -> None:
        self.s(key, subject, prop, sel=sel, value={"type": "time", "time": t.iso, "precision": t.precision}, **kw)

    def item(self, key: str, subject: str, prop: str, obj: str, *, sel: Mapping[str, Any], **kw: Any) -> None:
        self.s(key, subject, prop, sel=sel, object_qid=obj, **kw)

    def quantity(
        self, key: str, subject: str, prop: str, amount: int, unit: str, *, sel: Mapping[str, Any], **kw: Any
    ) -> None:
        self.s(key, subject, prop, sel=sel, value={"type": "quantity", "amount": str(amount), "unit": unit}, **kw)

    def point_in_time(self, key: str, subject: str, t: CodedTime, *, sel: Mapping[str, Any], **kw: Any) -> None:
        self.time(key, subject, "P585", t, sel=sel, **kw)


# =============================================================================== coded dates


@dataclass(frozen=True)
class CodedTime:
    """A provider date as coded: year (astronomical, >= 1 here) and month (None = unknown), Wikibase precision."""

    year: int
    month: int | None

    @property
    def precision(self) -> int:
        return 9 if self.month is None else 10

    @property
    def iso(self) -> str:
        return f"{self.year:04d}-{(self.month or 1):02d}-01"

    @property
    def start(self) -> date:
        return date(self.year, self.month or 1, 1)

    @property
    def end_exclusive(self) -> date:
        if self.month is None:
            return date(self.year + 1, 1, 1)
        return date(self.year + (self.month == 12), self.month % 12 + 1, 1)

    @property
    def last_day(self) -> date:
        return self.end_exclusive - timedelta(days=1)

    @property
    def text(self) -> str:
        return f"{self.year:04d}" if self.month is None else f"{self.year:04d}-{self.month:02d}"


def yyyymm(v: str | None, *, what: str) -> CodedTime | None:
    """ICOW ``YYYYMM`` (month ``00`` = unknown month; ``-9`` = not applicable). Any other shape is schema drift."""
    s = (v or "").strip()
    if s in ("", "-9", "."):
        return None
    if not s.isdigit() or len(s) < 3:
        raise drift(f"{what}: not a YYYYMM date: {s!r}")
    y, m = int(s[:-2]), int(s[-2:])
    if y < 1 or m > 12:
        raise drift(f"{what}: impossible YYYYMM date: {s!r}")
    return CodedTime(y, m or None)


def int_code(v: str | None, *, what: str, missing: Iterable[str] = ("", "-9", ".")) -> int | None:
    s = (v or "").strip()
    if s in set(missing):
        return None
    try:
        return int(s)
    except ValueError as e:
        raise drift(f"{what}: not an integer code: {s!r}") from e


# =============================================================================== CSV / TSV reading


@dataclass
class Table:
    rows: list[dict[str, str]]
    lines: list[int]

    def __iter__(self) -> Any:
        return iter(zip(self.lines, self.rows, strict=True))

    def __len__(self) -> int:
        return len(self.rows)


def read_delimited(
    body: bytes, *, name: str, columns: Sequence[str], delimiter: str, encoding: str = "utf-8-sig", min_rows: int = 1
) -> Table:
    """Rows of a delimited text body (declared encoding, strict), header checked, physical line numbers kept."""
    try:
        text = body.decode(encoding)
    except UnicodeDecodeError as e:
        raise drift(f"{name}: not {encoding}") from e
    rd = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header = [h.strip() for h in next(rd)]
    except StopIteration as e:
        raise drift(f"{name}: empty file", "incomplete") from e
    missing = [c for c in columns if c not in header]
    if missing:
        raise drift(f"{name}: missing columns {missing}")
    rows: list[dict[str, str]] = []
    lines: list[int] = []
    for rec in rd:
        if not any(v.strip() for v in rec):
            continue
        if len(rec) != len(header):
            raise drift(f"{name}: line {rd.line_num} has {len(rec)} fields, header {len(header)}")
        rows.append({h: v.strip() for h, v in zip(header, rec, strict=True)})
        lines.append(rd.line_num)
    if len(rows) < min_rows:
        raise drift(f"{name}: {len(rows)} rows < {min_rows} (truncated?)", "incomplete")
    return Table(rows, lines)


def zip_members(body: bytes, *, name: str, max_total: int = 64 * 1024 * 1024) -> dict[str, bytes]:
    """Every regular member of a zip (bounded total size; directories skipped; no path is written to disk)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise drift(f"{name}: not a zip archive (an HTML challenge page instead of the file?)") from e
    out: dict[str, bytes] = {}
    total = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            total += info.file_size
            if total > max_total:
                raise drift(f"{name}: members exceed {max_total} bytes", "too_large")
            out[info.filename] = zf.read(info)
    return out


def member(members: Mapping[str, bytes], suffix: str, *, name: str) -> tuple[str, bytes]:
    hits = [(k, v) for k, v in members.items() if k.endswith(suffix)]
    if len(hits) != 1:
        raise drift(f"{name}: expected one member ending {suffix!r}, found {sorted(members)}")
    return hits[0]


# =============================================================================== COLDAT

COLDAT_DOI = "doi:10.7910/DVN/T9SDEW"
COLDAT_VERSION = "3.0"
DV_HOSTS = ("dataverse.harvard.edu", "dvn-cloud-iqss.s3.amazonaws.com")
DV_VERSION_URL = "https://dataverse.harvard.edu/api/datasets/:persistentId/versions/{version}?persistentId={doi}"
DV_FILE_URL = "https://dataverse.harvard.edu/api/access/datafile/{}"
DV_LICENCE = "CC0 1.0"
COLDAT_REVISION = f"{COLDAT_DOI}@{COLDAT_VERSION}"
COLDAT_CITATION = (
    "Becker, Bastian (2019), Introducing COLDAT: The Colonial Dates Dataset, SOCIUM SFB 1342 WorkingPapers No. 2; "
    "Becker, Bastian, Colonial Dates Dataset (COLDAT), Harvard Dataverse, doi:10.7910/DVN/T9SDEW, V3"
)


@dataclass(frozen=True)
class DvFile:
    key: str
    file_id: int
    file_name: str
    media: tuple[str, ...]


COLDAT_FILES = (
    DvFile("dyads", 7416947, "COLDAT_dyads.tab", ("text/tab-separated-values", "text/plain", "application/octet-stream")),
    DvFile(
        "colonies", 7416946, "COLDAT_colonies.tab", ("text/tab-separated-values", "text/plain", "application/octet-stream")
    ),
    DvFile("readme", 7416948, "read_me.txt", ("text/plain", "application/octet-stream")),
)
#: The paper whose Appendix I is quoted (not fetched: its md5 must still be listed in the pinned version record).
COLDAT_CODEBOOK = {
    "file_id": 3476840,
    "file_name": "Introducing COLDAT.pdf",
    "md5": "31f7a831f4125d70497d6d0922094615",
    "sha256": "see registry/causal/relations_colonial.yaml documents.coldat_paper",
}

#: COLDAT colonizer key -> (label as the paper writes it, COW state number). The COW numbers are REVA's mapping of the
#: eight "European colonial powers" COLDAT builds on the Correlates of War data base (paper section 2.1): Belgium 211,
#: United Kingdom 200, France 220, Germany 255, Italy 325, Netherlands 210, Portugal 235, Spain 230 (COW State System
#: Membership list). The KG object is that code resolved at the spell's first year (e.g. Germany 1884 -> German
#: Empire Q43287, Italy 1889 -> Kingdom of Italy Q172579; before 1816 the crosswalk's single-entity rule applies).
COLONIZERS: dict[str, tuple[str, int]] = {
    "belgium": ("Belgium", 211),
    "britain": ("Britain", 200),
    "france": ("France", 220),
    "germany": ("Germany", 255),
    "italy": ("Italy", 325),
    "netherlands": ("Netherlands", 210),
    "portugal": ("Portugal", 235),
    "spain": ("Spain", 230),
}
COLDAT_AGGS = ("max", "mean")

#: Verbatim definitions (paper, Appendix I "Variable Description", long format).
COLDAT_DEFINITIONS = {
    "country": "Name of contemporary nation state.",
    "colonizer": "Name of the colonial power.",
    "col": "Dummy indicating whether country was ever colonized by colonizer.",
    "colstart_max": "First year during which country was colonized by colonizer. Based on last date aggregation.",
    "colend_max": "Last year during which country was colonized by colonizer. Based on last date aggregation.",
    "colstart_mean": "First year during which country was colonized by colonizer. Based on mean aggregation.",
    "colend_mean": "Last year during which country was colonized by colonizer. Based on mean aggregation.",
    "colony": (
        "A colony is a territory whose domestic and/or foreign affairs are dominated by a European nation state, and "
        "whose population is constructed as inferior to the colonizer."
    ),
    "aggregation": (
        "Instead of imposing a choice on researchers, I include mean aggregates, which aim at measurement accuracy, "
        "as well as last date aggregates, which aim at event consensuality, in COLDAT."
    ),
}
COLDAT_DATA_END = date(2017, 1, 1)  # "all members of the nation state system on December 31, 2016" (paper, 2.1)

_COLDAT_DYAD_COLS = ("country", "colonizer", "col", "colstart_max", "colend_max", "colstart_mean", "colend_mean")


def check_dataverse_version(meta_body: bytes, data: Mapping[int, bytes], *, title: str) -> dict[str, Any]:
    """Pinned version, CC0 licence, every fetched file listed with the received size, codebook md5 listed."""
    try:
        doc = json.loads(meta_body)
    except ValueError as e:
        raise drift(f"{title}: version record is not JSON") from e
    d = doc.get("data") or {}
    if f"{d.get('versionNumber')}.{d.get('versionMinorNumber')}" != COLDAT_VERSION:
        raise drift(f"{title}: Dataverse returned another version")
    lic = (d.get("license") or {}).get("name")
    if lic != DV_LICENCE:
        raise drift(f"{title}: licence changed: {lic!r}", "licence_change")
    files = {x["dataFile"]["id"]: x["dataFile"] for x in d.get("files", []) if "dataFile" in x}
    sizes: dict[str, Any] = {}
    for f in COLDAT_FILES:
        df = files.get(f.file_id)
        if df is None or df.get("filename") != f.file_name:
            raise drift(f"{title}: {f.file_name} (id {f.file_id}) not in the pinned version")
        body = data.get(f.file_id)
        if body is not None and df.get("filesize") is not None and int(df["filesize"]) != len(body):
            raise drift(f"{title}: {f.file_name} is {len(body)} bytes, version says {df['filesize']}", "checksum")
        if body is not None and not df.get("tabularData", False) and df.get("md5"):
            if hashlib.md5(body, usedforsecurity=False).hexdigest() != df["md5"]:
                raise drift(f"{title}: {f.file_name} md5 differs from the version record", "checksum")
        sizes[f.file_name] = df.get("filesize")
    cb = files.get(int(COLDAT_CODEBOOK["file_id"]))
    if cb is None or cb.get("md5") != COLDAT_CODEBOOK["md5"]:
        raise drift(f"{title}: quoted paper {COLDAT_CODEBOOK['file_name']} not in the pinned version (md5)")
    return {"licence": lic, "version": COLDAT_VERSION, "release_time": d.get("releaseTime"), "file_sizes": sizes}


@dataclass(frozen=True)
class ColdatDyad:
    country: str
    colonizer: str
    col: int
    years: Mapping[str, int | None]  # colstart_max, colend_max, colstart_mean, colend_mean
    line: int


def _year(v: str, *, what: str) -> int | None:
    s = v.strip()
    if s in ("", "NA", "."):
        return None
    try:
        y = int(float(s))
    except ValueError as e:
        raise drift(f"{what}: not a year: {s!r}") from e
    if float(s) != y or not 1000 <= y <= 2100:
        raise drift(f"{what}: implausible year {s!r}")
    return y


def parse_coldat_dyads(body: bytes) -> list[ColdatDyad]:
    t = read_delimited(body, name="COLDAT_dyads.tab", columns=_COLDAT_DYAD_COLS, delimiter="\t", min_rows=8)
    out = []
    seen: set[tuple[str, str]] = set()
    for line, r in t:
        c = r["colonizer"]
        if c not in COLONIZERS:
            raise drift(f"COLDAT_dyads.tab line {line}: unknown colonizer {c!r}")
        if r["col"] not in ("0", "1"):
            raise drift(f"COLDAT_dyads.tab line {line}: col is not a 0/1 dummy: {r['col']!r}")
        k = (r["country"], c)
        if k in seen:
            raise drift(f"COLDAT_dyads.tab: duplicate dyad {k}")
        seen.add(k)
        years = {
            v: _year(r[v], what=f"COLDAT_dyads.tab line {line} {v}") for v in _COLDAT_DYAD_COLS[3:]
        }  # fmt: skip
        out.append(ColdatDyad(r["country"], c, int(r["col"]), years, line))
    return out


def parse_coldat_wide(body: bytes) -> dict[str, tuple[int, dict[str, str]]]:
    cols = ["country"]
    for c in COLONIZERS:
        cols += [f"col.{c}", f"colstart.{c}_max", f"colend.{c}_max", f"colstart.{c}_mean", f"colend.{c}_mean"]
    t = read_delimited(body, name="COLDAT_colonies.tab", columns=cols, delimiter="\t", min_rows=1)
    return {r["country"]: (line, r) for line, r in t}


def coldat_cross_check(dyads: Sequence[ColdatDyad], wide: Mapping[str, tuple[int, dict[str, str]]]) -> list[dict[str, Any]]:
    """Cell-by-cell comparison of the long and the wide file (the provider says they hold the same information).
    Differences are returned (and flagged on the atlas cells), never resolved."""
    out = []
    names = {"col": "col.{c}", "colstart_max": "colstart.{c}_max", "colend_max": "colend.{c}_max"}
    names |= {"colstart_mean": "colstart.{c}_mean", "colend_mean": "colend.{c}_mean"}
    for d in dyads:
        w = wide.get(d.country)
        if w is None:
            out.append({"country": d.country, "colonizer": d.colonizer, "issue": "country missing from wide file"})
            continue
        for long_col, wide_col in names.items():
            lv = str(d.col) if long_col == "col" else ("" if d.years[long_col] is None else str(d.years[long_col]))
            wv = w[1][wide_col.format(c=d.colonizer)].strip()
            wv = "" if wv in ("NA", ".") else wv
            if wv.endswith(".0"):
                wv = wv[:-2]
            if lv != wv:
                out.append(
                    {"country": d.country, "colonizer": d.colonizer, "variable": long_col, "long": lv, "wide": wv}
                )
    extra = sorted(set(wide) - {d.country for d in dyads})
    out += [{"country": c, "issue": "country only in the wide file"} for c in extra]
    return out


def coldat_coverage_start(dyads: Sequence[ColdatDyad]) -> int:
    ys = [y for d in dyads for k, y in d.years.items() if y is not None and k.startswith("colstart")]
    return min(ys) if ys else 1462


def coldat_atlas(
    dyads: Sequence[ColdatDyad], *, raw_sha256: str, disagreements: Sequence[Mapping[str, Any]] = ()
) -> AtlasRows:
    from reva.atlas.tables import json_obj

    rows = AtlasRows()
    cov_start = date(coldat_coverage_start(dyads), 1, 1)
    bad = {(x.get("country"), x.get("colonizer")) for x in disagreements}
    for d in dyads:
        label, cow = COLONIZERS[d.colonizer]
        base = {
            "country_name": d.country,
            "colonizer": d.colonizer,
            "colonizer_cow_code": cow,
            "coldat_version": COLDAT_VERSION,
        }
        if (d.country, d.colonizer) in bad:
            base["wide_file_differs"] = True

        def add(indicator: str, start: date, end: date, *, value: str | None, value_text: str | None, unit: str,
                flags: Mapping[str, Any], columns: Sequence[str]) -> None:  # fmt: skip
            rows.add(
                {
                    "source": "coldat",
                    "dataset": "dyads_v3",
                    "indicator": indicator,
                    "concept_id": None,
                    "iso3": None,
                    "provider_geo": d.country,
                    "entity_qid": None,
                    "period_start": start,
                    "period_end": end,
                    "value": value,
                    "value_text": value_text,
                    "unit": unit,
                    "flags": json_obj({**base, **flags}),
                    "raw_sha256": raw_sha256,
                    "selector": json_obj(file="COLDAT_dyads.tab", line=d.line, columns=list(columns)),
                }
            )

        add(
            f"colonized|colonizer={d.colonizer}",
            cov_start,
            COLDAT_DATA_END,
            value=str(d.col),
            value_text=None,
            unit="1",
            flags={
                "definition": COLDAT_DEFINITIONS["col"],
                "period_meaning": "dataset coverage (first coded colonial start year .. 2016-12-31): 'ever colonized'",
            },
            columns=["country", "colonizer", "col"],
        )
        for agg in COLDAT_AGGS:
            s, e = d.years[f"colstart_{agg}"], d.years[f"colend_{agg}"]
            if s is None and e is None:
                continue
            fl: dict[str, Any] = {
                "aggregation": "last date aggregation" if agg == "max" else "mean aggregation",
                "start_definition": COLDAT_DEFINITIONS[f"colstart_{agg}"],
                "end_definition": COLDAT_DEFINITIONS[f"colend_{agg}"],
                "first_year": s,
                "last_year": e,
            }
            if d.col == 0:
                fl["inconsistent"] = "dates coded while col = 0 (shown as reported)"
            cols = ["country", "colonizer", f"colstart_{agg}", f"colend_{agg}"]
            if s is not None and e is not None and s <= e:
                p0, p1 = date(s, 1, 1), date(e + 1, 1, 1)
            else:
                y = s if s is not None else e
                assert y is not None
                p0, p1 = date(y, 1, 1), date(y + 1, 1, 1)
                fl["period_meaning"] = (
                    "first year only (last year missing)" if e is None else
                    "last year only (first year missing)" if s is None else
                    "first year after last year in the provider data: period = first year only"
                )  # fmt: skip
            add(f"colonial_rule|colonizer={d.colonizer}|agg={agg}", p0, p1, value=None, value_text=label, unit="category",
                flags=fl, columns=cols)  # fmt: skip
            if s is not None:
                add(f"colonial_rule_start|colonizer={d.colonizer}|agg={agg}", p0, p1, value=str(s), value_text=None,
                    unit="year", flags=fl, columns=cols)  # fmt: skip
            if e is not None:
                add(f"colonial_rule_end|colonizer={d.colonizer}|agg={agg}", p0, p1, value=str(e), value_text=None,
                    unit="year", flags=fl, columns=cols)  # fmt: skip
    return rows


def coldat_kg(
    dyads: Sequence[ColdatDyad],
    *,
    raw_sha256: str,
    resolver: StateEntityResolver,
    qid_for_iso3: Mapping[str, str],
) -> Kg:
    """``coldat:colonized_by`` statements: present-day country -> colonizer, one per aggregation."""
    from reva.atlas.tables import country_iso3

    kg = Kg("coldat", raw_sha256, "COLDAT_dyads.tab")
    for d in dyads:
        if d.col != 1:
            continue
        iso3 = country_iso3(d.country, "coldat_name")
        subj = qid_for_iso3.get(iso3 or "") or (f"iso3:{iso3}" if iso3 else f"coldat:country:{slug(d.country)}")
        if not subj.startswith("Q"):
            kg.entity(subj, "country", d.country, "contemporary nation state as named by COLDAT")
        label, cow = COLONIZERS[d.colonizer]
        for agg in COLDAT_AGGS:
            s, e = d.years[f"colstart_{agg}"], d.years[f"colend_{agg}"]
            if s is None or e is None or s > e:
                continue  # incomplete spells stay in the atlas table only (flagged)
            obj = resolver.entity("cow", cow, date(s, 7, 1), label)
            kg.item(
                f"{iso3 or slug(d.country)}:{d.colonizer}:{agg}",
                subj,
                "coldat:colonized_by",
                obj,
                sel={"line": d.line, "columns": ["country", "colonizer", "col", f"colstart_{agg}", f"colend_{agg}"]},
                start=f"{s:04d}-01-01",
                start_precision=9,
                end=f"{e:04d}-01-01",
                end_precision=9,
                qualifiers={
                    "aggregation": "last date aggregation" if agg == "max" else "mean aggregation",
                    "colonizer_as_coded": d.colonizer,
                    "country_as_coded": d.country,
                    "colonizer_cow_code": cow,
                    "object_resolution": f"COW {cow} at {s}-07-01 (atlas_geopol_common.StateCodeCrosswalk)",
                    "definition": COLDAT_DEFINITIONS[f"colstart_{agg}"] + " / " + COLDAT_DEFINITIONS[f"colend_{agg}"],
                },
            )
    return kg


# =============================================================================== ICOW

ICOW_HOST = "www.paulhensel.org"
ICOW_COLHIST_URL = "https://www.paulhensel.org/Data/colhist.zip"
ICOW_CLAIMS_URL = "https://www.paulhensel.org/Data/ICOWprov.zip"
ICOW_TERMS = (
    "All ICOW data sets may be downloaded freely, but we request several professional courtesies from users: (1) "
    "Please do not redistribute ICOW data to other scholars. Instead, every user should be directed to this web site "
    "to download the latest officially released version of the data"
)
ICOW_COLHIST_CITATION = (
    'Paul R. Hensel (2018). "ICOW Colonial History Data Set, version 1.1." Available at '
    "<http://www.paulhensel.org/icowcol.html>."
)
ICOW_CLAIMS_CITATION = (
    'Bryan A. Frederick, Paul R. Hensel, and Christopher Macaulay (2017). "The Issue Correlates of War Territorial '
    'Claims Data, 1816-2001." Journal of Peace Research 54(1); ICOW Territorial Claims Data, provisional version 1.01.'
)
ICOW_COLHIST_REVISION = "icow-colonial-history-1.1"
ICOW_CLAIMS_REVISION = "icow-territorial-claims-provisional-1.01"
ICOW_COLHIST_MEMBER = "coldata110.csv"
ICOW_COLHIST_CODEBOOK = "colcode.pdf"
ICOW_CLAIMS_MEMBER = "ICOWprov101.csv"
ICOW_CLAIMS_MANUAL = "UsersManual.pdf"

_ICOW_COLS = (
    *("State", "Name", "ColRuler", "IndFrom", "IndDate", "IndViol", "IndType", "SecFrom", "SecDate", "SecViol"),
    *("Into", "IntoDate", "COWsys", "GWsys", "Notes"),
)
_CLAIM_COLS = (
    *("region", "claimdy", "claim", "name", "dyadnum", "chal", "tgt", "dyad", "begclaim", "endclaim", "resolved"),
    *("endviol", "icowsal", "icowsalc", "salchal", "saltgt", "saltan", "salint", "midsiss", "midsfat", "warsiss"),
    "version",
)

#: Verbatim value labels and definitions (ICOW Colonial History 1.1 codebook ``colcode.pdf``, "List of Variables").
ICOW_COL_DEFINITIONS = {
    "ColRuler": (
        "The colonial or imperial power that was most responsible for shaping the development of the entity (or "
        "entities) that became this modern state."
    ),
    "IndFrom": (
        "The COW country code for the state, empire, or other entity from which this state gained independence. This "
        "variable is coded as missing where the state did not gain independence from a COW system member (e.g., by "
        "unifying local units into a new state)."
    ),
    "IndDate": (
        "The date on which this state became independent -- i.e., acquired control of its own foreign policy, without "
        "being ruled by a foreign power. This takes the form YYYYMM, where the month is coded as 00 if ICOW does not "
        "currently have an estimated month when the state took its current form."
    ),
    "IndViol": (
        "An independence process is considered violent if it occurred through armed revolt by the entity, or if it "
        "occurred through armed conflict between the former ruler and another state (as when the entity in question "
        "is conquered or freed by a foreign power, or when it changed hands as part of a treaty ending a war)."
    ),
    "SecFrom": "The COW country code for the state from which this state seceded after independence.",
    "Into": "The COW country code for a state that absorbed this state after independence.",
}
ICOW_VIOL = {
    0: "No = The independence did not involve organized violence.",
    1: "Yes = The independence occurred through organized violence.",
}
ICOW_SEC_VIOL = {
    0: "No = The secession did not involve organized violence.",
    1: "Yes = The secession occurred through organized violence.",
}
ICOW_IND_TYPE = {
    1: "Formation",
    2: "Decolonization",
    3: "Secession",
    4: "Partition",
}
ICOW_IND_TYPE_DEF = {
    1: "The entity was formed from other entities that have no direct analog in the COW interstate system.",
    2: "The entity was a dependency ruled by a foreign power before achieving independence.",
    3: "The entity was part of another state before achieving independence, with the original state surviving in "
    "reduced form.",
    4: "The entity was partitioned out of another state as it achieved independence, with the original state not "
    "surviving.",
}
#: ICOW Territorial Claims provisional 1.01 user's manual, variable ``resolved`` / ``endviol`` / ``region``.
ICOW_RESOLVED = {
    1: "Dropped by Challenger",
    2: "Renounced by Challenger",
    4: "Bilateral",
    5: "Independence",
    6: "Actor Leaves System",
    7: "Military Conquest/Occupation",
    8: "Dropped by Target",
    9: "Renounced by Target",
    10: "Plebiscite",
    11: "Claim No Longer Relevant",
    12: "Binding Third Party Decision",
    13: "Non-binding Third Party Activity",
    14: "Peace conference",
}
ICOW_ENDVIOL = {
    0: "No (claim ending did not involve organized violence)",
    1: "Yes (claim ended through organized violence)",
}
ICOW_REGION = {1: "Western Hemisphere", 2: "Europe", 3: "Africa", 4: "Middle East", 5: "Asia and Oceania"}
ICOW_CLAIMS_ONGOING = "200199"  # "Ongoing at current end of data set (12/31/2001)"


def independence_key(state: int, former_ruler: int) -> str:
    """Shared identifier of an independence (date-free): the same value on ICOW and COW Territorial Change records
    of the independence of COW state ``state`` from COW state ``former_ruler``."""
    return f"independence|cow:{state}|from|cow:{former_ruler}"


@dataclass(frozen=True)
class IcowState:
    line: int
    code: int
    name: str
    col_ruler: int | None
    ind_from: int | None
    ind_date: CodedTime
    ind_viol: int
    ind_type: int
    sec_from: int | None
    sec_date: CodedTime | None
    sec_viol: int | None
    into: int | None
    into_date: CodedTime | None
    cow_sys: CodedTime | None
    gw_sys: CodedTime | None
    notes: str | None


def parse_icow_colhist(body: bytes) -> list[IcowState]:
    t = read_delimited(body, name=ICOW_COLHIST_MEMBER, columns=_ICOW_COLS, delimiter=",", min_rows=10)
    out: list[IcowState] = []
    seen: set[int] = set()
    for line, r in t:
        w = f"{ICOW_COLHIST_MEMBER} line {line}"
        code = int_code(r["State"], what=f"{w} State")
        ind = yyyymm(r["IndDate"], what=f"{w} IndDate")
        viol = int_code(r["IndViol"], what=f"{w} IndViol")
        typ = int_code(r["IndType"], what=f"{w} IndType")
        if code is None or ind is None or viol not in ICOW_VIOL or typ not in ICOW_IND_TYPE:
            raise drift(f"{w}: State/IndDate/IndViol/IndType outside the codebook")
        if code in seen:
            raise drift(f"{w}: duplicate State {code}")
        seen.add(code)
        sv = int_code(r["SecViol"], what=f"{w} SecViol")
        if sv is not None and sv not in ICOW_SEC_VIOL:
            raise drift(f"{w}: SecViol {sv} outside the codebook")
        notes = r["Notes"].strip()
        out.append(
            IcowState(
                line=line,
                code=code,
                name=r["Name"].strip(),
                col_ruler=int_code(r["ColRuler"], what=f"{w} ColRuler"),
                ind_from=int_code(r["IndFrom"], what=f"{w} IndFrom"),
                ind_date=ind,
                ind_viol=viol,
                ind_type=typ,
                sec_from=int_code(r["SecFrom"], what=f"{w} SecFrom"),
                sec_date=yyyymm(r["SecDate"], what=f"{w} SecDate"),
                sec_viol=sv,
                into=int_code(r["Into"], what=f"{w} Into"),
                into_date=yyyymm(r["IntoDate"], what=f"{w} IntoDate"),
                cow_sys=yyyymm(r["COWsys"], what=f"{w} COWsys"),
                gw_sys=yyyymm(r["GWsys"], what=f"{w} GWsys"),
                notes=None if notes in ("", "-9") else notes,
            )
        )
    return out


@dataclass(frozen=True)
class IcowClaim:
    line: int
    claimdy: str
    claim: str
    name: str
    region: int
    chal: int
    tgt: int
    begin: CodedTime
    end: CodedTime | None  # None = ongoing at 2001-12-31
    resolved: int | None
    endviol: int | None
    icowsal: int | None
    salchal: int | None
    saltgt: int | None
    midsiss: int | None
    midsfat: int | None
    warsiss: int | None


def parse_icow_claims(body: bytes) -> list[IcowClaim]:
    t = read_delimited(body, name=ICOW_CLAIMS_MEMBER, columns=_CLAIM_COLS, delimiter=",", min_rows=10)
    out: list[IcowClaim] = []
    seen: set[str] = set()
    for line, r in t:
        w = f"{ICOW_CLAIMS_MEMBER} line {line}"
        cd = r["claimdy"].strip()
        if not cd.isdigit() or cd in seen:
            raise drift(f"{w}: claimdy {cd!r} missing or repeated")
        seen.add(cd)
        chal, tgt = int_code(r["chal"], what=f"{w} chal"), int_code(r["tgt"], what=f"{w} tgt")
        region = int_code(r["region"], what=f"{w} region")
        begin = yyyymm(r["begclaim"], what=f"{w} begclaim")
        if chal is None or tgt is None or begin is None or region not in ICOW_REGION:
            raise drift(f"{w}: chal/tgt/begclaim/region outside the manual")
        end = None if r["endclaim"].strip() == ICOW_CLAIMS_ONGOING else yyyymm(r["endclaim"], what=f"{w} endclaim")
        res = int_code(r["resolved"], what=f"{w} resolved")
        if res is not None and res not in ICOW_RESOLVED:
            raise drift(f"{w}: resolved {res} outside the manual")
        ev = int_code(r["endviol"], what=f"{w} endviol")
        out.append(
            IcowClaim(
                line=line,
                claimdy=cd,
                claim=r["claim"].strip(),
                name=r["name"].strip(),
                region=region,
                chal=chal,
                tgt=tgt,
                begin=begin,
                end=end,
                resolved=res,
                endviol=ev,
                icowsal=int_code(r["icowsal"], what=f"{w} icowsal"),
                salchal=int_code(r["salchal"], what=f"{w} salchal"),
                saltgt=int_code(r["saltgt"], what=f"{w} saltgt"),
                midsiss=int_code(r["midsiss"], what=f"{w} midsiss"),
                midsfat=int_code(r["midsfat"], what=f"{w} midsfat"),
                warsiss=int_code(r["warsiss"], what=f"{w} warsiss"),
            )
        )
    return out


class CodeNames:
    """COW code -> name: the provider's own names (ICOW ``Name`` of the state's row), else the crosswalk's label."""

    def __init__(self, crosswalk: StateCodeCrosswalk, names: Mapping[int, str]) -> None:
        self.cw = crosswalk
        self.names = dict(names)

    def label(self, code: int, when: date) -> str:
        if code in self.names:
            return self.names[code]
        r = self.cw.resolve("cow", code, when)
        return r.label or f"COW {code}"


def _resolve_cells(cw: StateCodeCrosswalk, code: int, when: date) -> dict[str, Any]:
    r = cw.resolve("cow", code, when)
    out: dict[str, Any] = {"code_resolution": r.reason}
    if r.iso3 is None and r.label:
        out["historical_state"] = r.label
    return out | {"_iso3": r.iso3, "_qid": r.qid}


def icow_colhist_atlas(states: Sequence[IcowState], *, raw_sha256: str, crosswalk: StateCodeCrosswalk) -> AtlasRows:
    """Per COW state, the coded colonial-history cells (provider values; dates = the coded month or year)."""
    from reva.atlas.tables import json_obj

    names = CodeNames(crosswalk, {s.code: s.name for s in states})
    rows = AtlasRows()
    for s in states:

        def add(indicator: str, t: CodedTime, *, value: str | None = None, value_text: str | None = None,
                unit: str, flags: Mapping[str, Any], columns: Sequence[str]) -> None:  # fmt: skip
            res = _resolve_cells(crosswalk, s.code, t.last_day)
            fl = {k: v for k, v in res.items() if not k.startswith("_")}
            fl |= {"state_name": s.name, "date_as_coded": t.text, "date_precision": "month" if t.month else "year"}
            rows.add(
                {
                    "source": "icow",
                    "dataset": "colhist_v11",
                    "indicator": indicator,
                    "concept_id": None,
                    "iso3": res["_iso3"],
                    "provider_geo": f"cow:{s.code}",
                    "entity_qid": res["_qid"],
                    "period_start": t.start,
                    "period_end": t.end_exclusive,
                    "value": value,
                    "value_text": value_text,
                    "unit": unit,
                    "flags": json_obj({**fl, **flags}),
                    "raw_sha256": raw_sha256,
                    "selector": json_obj(file=ICOW_COLHIST_MEMBER, line=s.line, columns=list(columns)),
                }
            )

        t = s.ind_date
        note = {"coder_note": s.notes} if s.notes else {}
        add("independence_year", t, value=str(t.year), unit="year",
            flags={"definition": ICOW_COL_DEFINITIONS["IndDate"], **note}, columns=["State", "IndDate", "Notes"])  # fmt: skip
        if s.ind_from is not None:
            add("independence_from", t, value_text=f"cow:{s.ind_from} {names.label(s.ind_from, t.start)}", unit="category",
                flags={"code": s.ind_from, "definition": ICOW_COL_DEFINITIONS["IndFrom"],
                       "former_ruler_iso3": crosswalk.resolve("cow", s.ind_from, t.start).iso3},
                columns=["State", "IndFrom", "IndDate"])  # fmt: skip
        add("independence_violent", t, value=str(s.ind_viol), unit="1",
            flags={"label": ICOW_VIOL[s.ind_viol], "definition": ICOW_COL_DEFINITIONS["IndViol"],
                   "not_a_causal_claim": "the codebook does not identify the violent episode (registry/causal/relations_colonial.yaml)"},
            columns=["State", "IndViol", "IndDate"])  # fmt: skip
        add("independence_type", t, value_text=ICOW_IND_TYPE[s.ind_type], unit="category",
            flags={"code": s.ind_type, "definition": ICOW_IND_TYPE_DEF[s.ind_type]},
            columns=["State", "IndType", "IndDate"])  # fmt: skip
        if s.col_ruler is not None:
            add("primary_colonial_ruler", t, value_text=f"cow:{s.col_ruler} {names.label(s.col_ruler, t.start)}",
                unit="category",
                flags={"code": s.col_ruler, "definition": ICOW_COL_DEFINITIONS["ColRuler"],
                       "period_meaning": "attached to the independence date (ICOW codes no colonial-rule years)",
                       "ruler_iso3": crosswalk.resolve("cow", s.col_ruler, t.start).iso3},
                columns=["State", "ColRuler", "IndDate"])  # fmt: skip
        if s.sec_date is not None:
            if s.sec_from is not None:
                add("seceded_from", s.sec_date, value_text=f"cow:{s.sec_from} {names.label(s.sec_from, s.sec_date.start)}",
                    unit="category", flags={"code": s.sec_from, "definition": ICOW_COL_DEFINITIONS["SecFrom"]},
                    columns=["State", "SecFrom", "SecDate"])  # fmt: skip
            add("secession_year", s.sec_date, value=str(s.sec_date.year), unit="year", flags={},
                columns=["State", "SecDate"])  # fmt: skip
            if s.sec_viol is not None:
                add("secession_violent", s.sec_date, value=str(s.sec_viol), unit="1",
                    flags={"label": ICOW_SEC_VIOL[s.sec_viol]}, columns=["State", "SecViol", "SecDate"])  # fmt: skip
        if s.into is not None and s.into_date is not None:
            add("merged_into", s.into_date, value_text=f"cow:{s.into} {names.label(s.into, s.into_date.start)}",
                unit="category", flags={"code": s.into, "definition": ICOW_COL_DEFINITIONS["Into"]},
                columns=["State", "Into", "IntoDate"])  # fmt: skip
        for ind, tt, col, what in (("cow_system_entry", s.cow_sys, "COWsys", "COW system list 2004.1"),
                                   ("gw_system_entry", s.gw_sys, "GWsys", "Gleditsch & Ward list, updated through 2002")):  # fmt: skip
            if tt is not None:
                add(ind, tt, value=str(tt.year), unit="year", flags={"as_coded_by_icow": what},
                    columns=["State", col])  # fmt: skip
    return rows


def icow_claims_atlas(claims: Sequence[IcowClaim], *, raw_sha256: str, crosswalk: StateCodeCrosswalk) -> AtlasRows:
    """One row per (dyadic claim, claimant role): value_text = claimed territory, period = coded claim interval."""
    from reva.atlas.tables import json_obj

    rows = AtlasRows()
    for c in claims:
        end = c.end.end_exclusive if c.end is not None else date(2002, 1, 1)
        for role, code, other in (("challenger", c.chal, c.tgt), ("target", c.tgt, c.chal)):
            res = _resolve_cells(crosswalk, code, c.begin.last_day)
            fl = {k: v for k, v in res.items() if not k.startswith("_")}
            fl |= {
                "claimdy": c.claimdy,
                "claim": c.claim,
                "role": role,
                "other_claimant": f"cow:{other}",
                "region": ICOW_REGION[c.region],
                "begin_as_coded": c.begin.text,
                "end_as_coded": c.end.text if c.end else "ongoing (200199: ongoing at 12/31/2001)",
                "resolved": None if c.resolved is None else f"{c.resolved}: {ICOW_RESOLVED[c.resolved]}",
                "endviol": None if c.endviol is None else ICOW_ENDVIOL.get(c.endviol, str(c.endviol)),
                "icowsal": c.icowsal,
                "salience_own": c.salchal if role == "challenger" else c.saltgt,
                "midsiss": c.midsiss,
                "midsfat": c.midsfat,
                "warsiss": c.warsiss,
                "left_censored": c.begin.text == "1816-01",
            }
            rows.add(
                {
                    "source": "icow",
                    "dataset": "claims_prov101",
                    "indicator": f"territorial_claim|claimdy={c.claimdy}|role={role}",
                    "concept_id": None,
                    "iso3": res["_iso3"],
                    "provider_geo": f"cow:{code}",
                    "entity_qid": res["_qid"],
                    "period_start": c.begin.start,
                    "period_end": end,
                    "value": None,
                    "value_text": c.name,
                    "unit": "name",
                    "flags": json_obj(fl),
                    "raw_sha256": raw_sha256,
                    "selector": json_obj(
                        file=ICOW_CLAIMS_MEMBER, line=c.line, columns=["claimdy", "name", "chal", "tgt", "begclaim", "endclaim"]
                    ),
                }
            )
    return rows


def icow_colhist_kg(states: Sequence[IcowState], *, raw_sha256: str, resolver: StateEntityResolver) -> Kg:
    names = CodeNames(resolver.crosswalk, {s.code: s.name for s in states})
    kg = Kg("icowcol", raw_sha256, ICOW_COLHIST_MEMBER)

    def ent(code: int, when: date) -> str:
        return resolver.entity("cow", code, when, names.label(code, when))

    for s in states:
        sel = {"line": s.line}
        t = s.ind_date
        state = ent(s.code, t.last_day)
        ev = f"icowcol:independence:{s.code}"
        kg.entity(ev, "event", f"Independence of {s.name} (ICOW)", f"independence of COW state {s.code} as coded by ICOW Colonial History 1.1")  # fmt: skip
        kg.string(f"{s.code}:independence:type_of_event", ev, "colonial:event_type", "independence", sel=sel)
        kg.point_in_time(f"{s.code}:independence:date", ev, t, sel={**sel, "column": "IndDate"},
                         qualifiers={"as_coded": t.text})  # fmt: skip
        kg.item(f"{s.code}:independence:of", ev, "icowcol:independence_of", state, sel={**sel, "column": "State"})
        if s.ind_from is not None:
            kg.item(f"{s.code}:independence:from", ev, "icowcol:independence_from", ent(s.ind_from, t.start),
                    sel={**sel, "column": "IndFrom"}, qualifiers={"code": s.ind_from})  # fmt: skip
            kg.string(f"{s.code}:independence:key", ev, "colonial:independence_key", independence_key(s.code, s.ind_from),
                      sel={**sel, "column": ["State", "IndFrom"]},
                      qualifiers={"method": "shared COW codes (state, former ruler); date-free"})  # fmt: skip
        kg.string(f"{s.code}:independence:violent", ev, "icowcol:independence_violent", ICOW_VIOL[s.ind_viol],
                  sel={**sel, "column": "IndViol"}, qualifiers={"code": s.ind_viol})  # fmt: skip
        kg.string(f"{s.code}:independence:type", ev, "icowcol:independence_type", ICOW_IND_TYPE[s.ind_type],
                  sel={**sel, "column": "IndType"},
                  qualifiers={"code": s.ind_type, "definition": ICOW_IND_TYPE_DEF[s.ind_type]})  # fmt: skip
        if s.notes:
            kg.string(f"{s.code}:independence:note", ev, "icowcol:coder_note", s.notes, sel={**sel, "column": "Notes"})
        if s.col_ruler is not None:
            kg.item(f"{s.code}:primary_colonial_ruler", state, "icowcol:primary_colonial_ruler", ent(s.col_ruler, t.start),
                    sel={**sel, "column": "ColRuler"},
                    qualifiers={"code": s.col_ruler, "definition": ICOW_COL_DEFINITIONS["ColRuler"],
                                "note": "ICOW codes no years of colonial rule (version 1.1)"})  # fmt: skip
        for prop, tt, col in (("icowcol:cow_system_entry", s.cow_sys, "COWsys"), ("icowcol:gw_system_entry", s.gw_sys, "GWsys")):  # fmt: skip
            if tt is not None:
                kg.time(f"{s.code}:{col}", state, prop, tt, sel={**sel, "column": col})
        if s.sec_date is not None:
            sev = f"icowcol:secession:{s.code}"
            kg.entity(sev, "event", f"Secession of {s.name} (ICOW)", f"secession of COW state {s.code} as coded by ICOW Colonial History 1.1")  # fmt: skip
            kg.string(f"{s.code}:secession:type_of_event", sev, "colonial:event_type", "secession", sel=sel)
            kg.point_in_time(f"{s.code}:secession:date", sev, s.sec_date, sel={**sel, "column": "SecDate"},
                             qualifiers={"as_coded": s.sec_date.text})  # fmt: skip
            kg.item(f"{s.code}:secession:of", sev, "icowcol:secession_of", ent(s.code, s.sec_date.last_day),
                    sel={**sel, "column": "State"})  # fmt: skip
            if s.sec_from is not None:
                kg.item(f"{s.code}:secession:from", sev, "icowcol:seceded_from", ent(s.sec_from, s.sec_date.start),
                        sel={**sel, "column": "SecFrom"}, qualifiers={"code": s.sec_from})  # fmt: skip
            if s.sec_viol is not None:
                kg.string(f"{s.code}:secession:violent", sev, "icowcol:secession_violent", ICOW_SEC_VIOL[s.sec_viol],
                          sel={**sel, "column": "SecViol"}, qualifiers={"code": s.sec_viol})  # fmt: skip
        if s.into is not None and s.into_date is not None:
            mev = f"icowcol:merger:{s.code}"
            kg.entity(mev, "event", f"Merger of {s.name} into another state (ICOW)", f"merger of COW state {s.code} as coded by ICOW Colonial History 1.1")  # fmt: skip
            kg.string(f"{s.code}:merger:type_of_event", mev, "colonial:event_type", "merger", sel=sel)
            kg.point_in_time(f"{s.code}:merger:date", mev, s.into_date, sel={**sel, "column": "IntoDate"},
                             qualifiers={"as_coded": s.into_date.text})  # fmt: skip
            kg.item(f"{s.code}:merger:of", mev, "icowcol:merger_of", ent(s.code, s.into_date.start),
                    sel={**sel, "column": "State"})  # fmt: skip
            kg.item(f"{s.code}:merger:into", mev, "icowcol:merged_into", ent(s.into, s.into_date.last_day),
                    sel={**sel, "column": "Into"}, qualifiers={"code": s.into})  # fmt: skip
    return kg


def icow_claims_kg(
    claims: Sequence[IcowClaim], *, raw_sha256: str, resolver: StateEntityResolver, names: Mapping[int, str]
) -> Kg:
    cn = CodeNames(resolver.crosswalk, names)
    kg = Kg("icowclaim", raw_sha256, ICOW_CLAIMS_MEMBER)
    for c in claims:
        sel = {"line": c.line}
        ev = f"icowclaim:{c.claimdy}"
        a, b = cn.label(c.chal, c.begin.start), cn.label(c.tgt, c.begin.start)
        kg.entity(ev, "event", f"Territorial claim: {c.name} ({a} v. {b})", f"ICOW dyadic territorial claim {c.claimdy} (claim {c.claim})")  # fmt: skip
        k = c.claimdy
        kg.string(f"{k}:type_of_event", ev, "colonial:event_type", "territorial_claim", sel=sel)
        kg.time(f"{k}:begin", ev, "P580", c.begin, sel={**sel, "column": "begclaim"},
                qualifiers={"as_coded": c.begin.text, **({"left_censored": "data begin 1816-01"} if c.begin.text == "1816-01" else {})})  # fmt: skip
        if c.end is not None:
            kg.time(f"{k}:end", ev, "P582", c.end, sel={**sel, "column": "endclaim"}, qualifiers={"as_coded": c.end.text})
        kg.item(f"{k}:challenger", ev, "icowclaim:challenger", resolver.entity("cow", c.chal, c.begin.last_day, a),
                sel={**sel, "column": "chal"}, qualifiers={"code": c.chal})  # fmt: skip
        kg.item(f"{k}:target", ev, "icowclaim:target", resolver.entity("cow", c.tgt, c.begin.last_day, b),
                sel={**sel, "column": "tgt"}, qualifiers={"code": c.tgt})  # fmt: skip
        kg.string(f"{k}:territory", ev, "icowclaim:territory", c.name, sel={**sel, "column": "name"})
        kg.string(f"{k}:claim", ev, "icowclaim:claim_number", c.claim, sel={**sel, "column": "claim"})
        kg.string(f"{k}:region", ev, "icowclaim:region", ICOW_REGION[c.region], sel={**sel, "column": "region"})
        if c.resolved is not None:
            kg.string(f"{k}:resolution", ev, "icowclaim:resolution", ICOW_RESOLVED[c.resolved],
                      sel={**sel, "column": "resolved"}, qualifiers={"code": c.resolved})  # fmt: skip
        else:
            kg.string(f"{k}:resolution", ev, "icowclaim:resolution", "Ongoing (not resolved at 12/31/2001)",
                      sel={**sel, "column": "resolved"}, qualifiers={"code": -9})  # fmt: skip
        if c.endviol in ICOW_ENDVIOL:
            kg.string(f"{k}:endviol", ev, "icowclaim:ended_through_violence", ICOW_ENDVIOL[c.endviol],
                      sel={**sel, "column": "endviol"}, qualifiers={"code": c.endviol})  # fmt: skip
        for prop, v, col, unit in (("icowclaim:salience", c.icowsal, "icowsal", "index 0-12"),
                                   ("icowclaim:mids_over_issue", c.midsiss, "midsiss", "count"),
                                   ("icowclaim:fatal_mids_over_issue", c.midsfat, "midsfat", "count"),
                                   ("icowclaim:wars_over_issue", c.warsiss, "warsiss", "count")):  # fmt: skip
            if v is not None:
                kg.quantity(f"{k}:{col}", ev, prop, v, unit, sel={**sel, "column": col})
    return kg


def code_entities_and_links(
    resolver: StateEntityResolver, *, raw_sha256: str, prefix: str
) -> tuple[list[KgEntity], list[KgStatement]]:
    """State-code entities (``cow:<code>``) the resolver had to create, and their dated
    ``geopol:corresponds_to_iso3`` statements (keys prefixed per dataset: each load owns its own)."""
    from dataclasses import replace

    from reva.adapters.connectors.atlas_geopol_common import code_correspondence

    links = [replace(s, key=f"{prefix}:{s.key}") for s in code_correspondence(resolver, raw_sha256=raw_sha256)]
    return resolver.code_entities(), links


# =============================================================================== atlas source specs

SOURCE_KEY_COLDAT = "harvard.dataverse.coldat"
SOURCE_KEY_ICOW = "icow.paulhensel"
LICENCE_COLDAT = "CC0 1.0 (Harvard Dataverse doi:10.7910/DVN/T9SDEW V3 license record)"
LICENCE_ICOW = (
    "ICOW terms (not an open licence): free download; cite; 'Please do not redistribute ICOW data to other scholars' "
    "(professional courtesy). Flagged for maintainer review"
)


def atlas_source() -> list[Any]:
    from reva.atlas.sources import AtlasSourceSpec

    return [
        AtlasSourceSpec(
            name="atlas.coldat",
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY_COLDAT,
            atlas_source="coldat",
            provider="Bastian Becker (University of Bremen, SOCIUM) via Harvard Dataverse",
            licence=LICENCE_COLDAT,
            allowed_hosts=(*DV_HOSTS, "cran.r-project.org"),
            datasets=("dyads_v3",),
            dataset_ids=(f"{COLDAT_DOI}@{COLDAT_VERSION}",),
            cadence_seconds=180 * 86400,
            planned_requests=7,
            max_response_bytes=5_000_000,
            min_interval_seconds=1.0,
            parser_version=PARSER_VERSION_COLDAT,
            notes="COLDAT v3.0: version record + dyads/colonies .tab + read_me + countrycode tarball (5 requests)",
        ),
        AtlasSourceSpec(
            name="atlas.icow",
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY_ICOW,
            atlas_source="icow",
            provider="Issue Correlates of War (ICOW) Project, Paul R. Hensel (University of North Texas)",
            licence=LICENCE_ICOW,
            allowed_hosts=(ICOW_HOST, "cran.r-project.org"),
            datasets=("colhist_v11", "claims_prov101"),
            dataset_ids=("ICOW Colonial History 1.1", "ICOW Territorial Claims provisional 1.01"),
            cadence_seconds=180 * 86400,
            planned_requests=5,
            max_response_bytes=10_000_000,
            min_interval_seconds=2.0,
            parser_version=PARSER_VERSION_ICOW,
            notes="colhist.zip + ICOWprov.zip (paulhensel.org) + countrycode 1.9.0 tarball (CRAN): 3 requests",
        ),
    ]


# =============================================================================== ingest


RELATIONS_FILE = "causal/relations_colonial.yaml"


def relations() -> dict[str, Any]:
    """``registry/causal/relations_colonial.yaml`` (owner A-COLONIAL): pinned codebook documents and the mapping of
    every coded variable (all non-causal: attribute / context)."""
    import yaml

    from reva.ontology.registry import REGISTRY_ROOT

    doc = yaml.safe_load((REGISTRY_ROOT / RELATIONS_FILE).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("owner") != "A-COLONIAL":
        raise ValueError("registry/causal/relations_colonial.yaml: unexpected content")
    return doc


def check_document(key: str, body: bytes) -> dict[str, Any]:
    """A codebook shipped inside a provider archive vs its pinned sha256. A changed codebook stops the dataset
    (the quoted value labels may no longer be the provider's)."""
    pinned = relations()["documents"][key]["sha256"]
    got = sha256(body)
    if got != pinned:
        raise drift(f"codebook {key} changed (sha256 {got[:12]} != pinned {pinned[:12]}): re-check the quoted labels")
    return {"document": key, "sha256": got}


def _kg_ready(run: Any) -> bool:
    from reva.adapters.connectors.atlas_geopol_common import kg_available

    return getattr(run, "engine", None) is not None and kg_available(run.engine)


async def _ingest_coldat(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geopol_common import fetch_countrycode, load_qid_for_iso3

    if not run.wants("dyads_v3"):
        return {"skipped": "dataset not requested"}
    meta = await run.get(
        DV_VERSION_URL.format(version=COLDAT_VERSION, doi=COLDAT_DOI),
        key=f"coldat:version:{COLDAT_VERSION}",
        media_types=("application/json",),
        max_bytes=1_000_000,
    )
    raws: dict[str, Any] = {}
    for f in COLDAT_FILES:
        raws[f.key] = await run.get(
            DV_FILE_URL.format(f.file_id), key=f"coldat:{f.file_name}", media_types=f.media, max_bytes=2_000_000
        )
    check = check_dataverse_version(
        meta.body, {f.file_id: raws[f.key].body for f in COLDAT_FILES}, title="COLDAT v3.0"
    )
    dyads = parse_coldat_dyads(raws["dyads"].body)
    wide = parse_coldat_wide(raws["colonies"].body)
    disagreements = coldat_cross_check(dyads, wide)
    # parsing succeeded: archive (nothing is archived on schema drift)
    shas = {"version": run.archive(meta)} | {k: run.archive(v) for k, v in raws.items()}
    rows = coldat_atlas(dyads, raw_sha256=shas["dyads"], disagreements=disagreements)
    reg = run.register(
        dataset="dyads_v3",
        frame=rows.frame(),
        raw_sha256s=[shas["dyads"]],
        geo_scheme="coldat_name",
        coverage_note=(
            f"COLDAT v3.0 (CC0): {len({d.country for d in dyads})} contemporary states x 8 European colonizers, "
            f"{sum(d.col for d in dyads)} colonized dyads; both aggregations (max = last date, mean); "
            f"long/wide file differences: {len(disagreements)}"
        ),
        rights={
            "licence": LICENCE_COLDAT,
            "provider": "Bastian Becker / Harvard Dataverse",
            "citation": COLDAT_CITATION,
            "source_key": SOURCE_KEY_COLDAT,
        },
    )
    out: dict[str, Any] = {
        "version_check": check,
        "dyads": len(dyads),
        "colonized_dyads": sum(d.col for d in dyads),
        "wide_long_disagreements": disagreements[:50],
        "atlas": {"dyads_v3": {k: reg[k] for k in ("dataset_key", "row_count", "created")}},
        "claims": {"delivered": 0, "reason": "no COLDAT variable codes a causal relation (relations_colonial.yaml)"},
    }
    if _kg_ready(run):
        cw, cc_sha = await fetch_countrycode(run)
        qids = load_qid_for_iso3()
        resolver = StateEntityResolver(cw, qids)
        kg = coldat_kg(dyads, raw_sha256=shas["dyads"], resolver=resolver, qid_for_iso3=qids)
        code_ents, code_links = code_entities_and_links(resolver, raw_sha256=cc_sha, prefix="coldat")
        ents = [*kg.entities.values(), *code_ents]
        out["kg"] = load_colonial_kg(
            run.engine,
            run.store,
            ents,
            [*kg.statements, *code_links],
            source_key=SOURCE_KEY_COLDAT,
            source_revision=COLDAT_REVISION,
            raw_sha256s=[shas["dyads"], cc_sha],
            retrieved_at=raws["dyads"].retrieved_at,
            origin=run.origin,
        )
        out["kg_counts"] = {"entities": len(ents), "statements": len(kg.statements) + len(code_links)}
    return out


async def _ingest_icow(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geopol_common import fetch_countrycode, load_qid_for_iso3

    want_col, want_claims = run.wants("colhist_v11"), run.wants("claims_prov101")
    media = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
    out: dict[str, Any] = {"atlas": {}, "kg": {}, "kg_counts": {}, "documents": []}
    parsed: dict[str, Any] = {}
    if want_col:
        raw = await run.get(ICOW_COLHIST_URL, key="icow:colhist:1.1", media_types=media, max_bytes=5_000_000)
        mem = zip_members(raw.body, name="colhist.zip")
        _, cb = member(mem, "/" + ICOW_COLHIST_CODEBOOK, name="colhist.zip")
        out["documents"].append(check_document("icow_colcode", cb))
        _, data = member(mem, "/" + ICOW_COLHIST_MEMBER, name="colhist.zip")
        parsed["colhist"] = (raw, parse_icow_colhist(data))
    if want_claims:
        raw = await run.get(ICOW_CLAIMS_URL, key="icow:claims:prov1.01", media_types=media, max_bytes=8_000_000)
        mem = zip_members(raw.body, name="ICOWprov.zip")
        _, man = member(mem, "/" + ICOW_CLAIMS_MANUAL, name="ICOWprov.zip")
        out["documents"].append(check_document("icow_claims_manual", man))
        _, data = member(mem, "/" + ICOW_CLAIMS_MEMBER, name="ICOWprov.zip")
        parsed["claims"] = (raw, parse_icow_claims(data))
    if not parsed:
        return {"skipped": "dataset not requested"}
    cw, cc_sha = await fetch_countrycode(run)
    shas = {k: run.archive(v[0]) for k, v in parsed.items()}
    rights = {"licence": LICENCE_ICOW, "provider": "ICOW Project (Paul R. Hensel)", "source_key": SOURCE_KEY_ICOW}
    names: dict[int, str] = {}
    if "colhist" in parsed:
        states = parsed["colhist"][1]
        names = {s.code: s.name for s in states}
        rows = icow_colhist_atlas(states, raw_sha256=shas["colhist"], crosswalk=cw)
        reg = run.register(
            dataset="colhist_v11",
            frame=rows.frame(),
            raw_sha256s=[shas["colhist"]],
            coverage_note=f"ICOW Colonial History 1.1: {len(states)} COW states (1816-2018 members), as coded",
            rights={**rights, "citation": ICOW_COLHIST_CITATION},
        )
        out["atlas"]["colhist_v11"] = {k: reg[k] for k in ("dataset_key", "row_count", "created")}
        out["states"] = len(states)
    if "claims" in parsed:
        claims = parsed["claims"][1]
        rows = icow_claims_atlas(claims, raw_sha256=shas["claims"], crosswalk=cw)
        reg = run.register(
            dataset="claims_prov101",
            frame=rows.frame(),
            raw_sha256s=[shas["claims"]],
            coverage_note=f"ICOW Territorial Claims provisional 1.01: {len(claims)} dyadic claims 1816-2001, as coded",
            rights={**rights, "citation": ICOW_CLAIMS_CITATION},
        )
        out["atlas"]["claims_prov101"] = {k: reg[k] for k in ("dataset_key", "row_count", "created")}
        out["claims_dyads"] = len(claims)
    out["claims"] = {"delivered": 0, "reason": "no ICOW variable identifies a cause event (relations_colonial.yaml)"}
    if _kg_ready(run):
        qids = load_qid_for_iso3()
        for key, revision in (("colhist", ICOW_COLHIST_REVISION), ("claims", ICOW_CLAIMS_REVISION)):
            if key not in parsed:
                continue
            resolver = StateEntityResolver(cw, qids)
            if key == "colhist":
                kg = icow_colhist_kg(parsed[key][1], raw_sha256=shas[key], resolver=resolver)
            else:
                kg = icow_claims_kg(parsed[key][1], raw_sha256=shas[key], resolver=resolver, names=names)
            code_ents, code_links = code_entities_and_links(resolver, raw_sha256=cc_sha, prefix=key)
            ents = [*kg.entities.values(), *code_ents]
            out["kg"][key] = load_colonial_kg(
                run.engine,
                run.store,
                ents,
                [*kg.statements, *code_links],
                source_key=SOURCE_KEY_ICOW,
                source_revision=revision,
                raw_sha256s=[shas[key], cc_sha],
                retrieved_at=parsed[key][0].retrieved_at,
                origin=run.origin,
            )
            out["kg_counts"][key] = {"entities": len(ents), "statements": len(kg.statements) + len(code_links)}
    return out


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if run.spec.name == "atlas.coldat":
        return await _ingest_coldat(run)
    if run.spec.name == "atlas.icow":
        return await _ingest_icow(run)
    raise ValueError(f"atlas_colonial: unknown source {run.spec.name}")
