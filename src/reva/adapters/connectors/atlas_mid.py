"""COW Militarized Interstate Disputes (MID 5.0 disputes, 5.01 incidents) (agent CAUSDATA).

Source and access
-----------------
Correlates of War Project, MID data page ``correlatesofwar.org/data-sets/mids/``: two public zips, no account.

* ``MID-5-Data-and-Supporting-Materials.zip`` -- ``MIDA 5.0.csv`` (one row per dispute, 1816-2014),
  ``MIDB 5.0.csv`` (one row per participant spell), the Dispute and Incident Coding Manuals (PDF).
* ``Incident_Level_5.01.zip`` -- ``MIDI_5.01.csv`` / ``MIDIP_5.01.csv`` (incidents 1992-2014, v5.01 = incidents
  re-numbered in temporal order, ``ord_incidnum``) and the MID 5.0/5.01 codebook (PDF).

Terms: COW data are free to use with citation, users may not charge for access (COW FAQ) -- not an open licence;
fetched under A-HISTECON's registered source ``cow.data`` (same provider and terms). The three PDFs must match the
sha256 pinned in ``registry/causal/relations_datasets.yaml`` before claims are delivered.

Outputs (relation semantics and reasons: ``registry/causal/relations_datasets.yaml`` dataset ``mid5``)
-------
* KG events: ``mid:dispute:<dispnum>`` (militarized_interstate_dispute) with participants (``P710``; qualifiers
  side A/B, originator, revisionist, hostility level), dates, hostility / action / fatality levels; the coded
  **issue** (revision type sought by a revisionist state) as ``causdata:coded_issue`` -- context, NOT a cause;
  ``mid:dispute:<dispnum>:outcome`` (dispute_outcome); incidents ``mid:incident:<ord_incidnum>``
  (militarized_incident) ``P361`` part of the dispute and ``P155`` follows the previous incident (order, not cause).
* T1 claims: ``coded_outcome`` dispute -> outcome (Dispute Coding Manual outcome definitions). Side A / originator
  are participation attributes (side A is "ultimately arbitrary" by the manual's own words), never initiator claims.
* Atlas table ``cow.mid5_cy`` (COMPUTED counts per COW state code and year).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from itertools import pairwise
from typing import Any

from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors.atlas_geopol_common import (
    COUNTRYCODE_VERSION,
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    StateEntityResolver,
    atlas_frame,
    code_correspondence,
    fetch_countrycode,
    load_qid_for_iso3,
)
from reva.ports.connectors import RawAcquisition

SOURCE_KEY = "cow.data"
CONNECTOR_NAME = "atlas.mid"
CONNECTOR_VERSION = "1.0.1"  # 1.0.1 (AUDFIX L5): MID outcome quote completed ("took place.")
PARSER_VERSION = "atlas-mid-parse-1"
MID_VERSION = "5.0/5.01"
ATLAS_SOURCE = "cow"
HOST = "correlatesofwar.org"
BASE = f"https://{HOST}/wp-content/uploads/"
ZIP_MEDIA = ("application/zip", "application/octet-stream", "application/x-zip-compressed")
MAX_BYTES = 12_000_000

#: zip key -> (file name, {member key: (member file name, minimum rows or 0 for a document)})
ZIPS: dict[str, tuple[str, dict[str, tuple[str, int]]]] = {
    "mid5": (
        "MID-5-Data-and-Supporting-Materials.zip",
        {
            "mida": ("MIDA 5.0.csv", 2300),
            "midb": ("MIDB 5.0.csv", 5500),
            "mid_dispute_manual": ("Dispute Coding Manual.pdf", 0),
            "mid_incident_manual": ("Incident Coding Manual.pdf", 0),
        },
    ),
    "mid501": (
        "Incident_Level_5.01.zip",
        {
            "midi": ("MIDI_5.01.csv", 4000),
            "midip": ("MIDIP_5.01.csv", 9000),
            "mid_codebook": ("MID 5.0 Codebook.pdf", 0),
        },
    ),
}
REQUIRED = {
    "mida": (
        "dispnum",
        "stday",
        "stmon",
        "styear",
        "endday",
        "endmon",
        "endyear",
        "outcome",
        "settle",
        "fatality",
        "hiact",
        "hostlev",
        "recip",
        "version",
    ),
    "midb": (
        "dispnum",
        "stabb",
        "ccode",
        "stday",
        "stmon",
        "styear",
        "endday",
        "endmon",
        "endyear",
        "sidea",
        "revstate",
        "revtype1",
        "revtype2",
        "fatality",
        "hiact",
        "hostlev",
        "orig",
        "version",
    ),
    "midi": (
        "dispnum",
        "ord_incidnum",
        "stday",
        "stmon",
        "styear",
        "endday",
        "endmon",
        "endyear",
        "fatality",
        "action",
        "hostlev",
        "version",
        "incidnum",
    ),
    "midip": (
        "dispnum",
        "ord_incidnum",
        "stabb",
        "ccode",
        "stday",
        "stmon",
        "styear",
        "insidea",
        "sidea",
        "fatality",
        "action",
        "hostlev",
        "revtype1",
        "revtype2",
        "version",
        "incidnum",
    ),
}
DOCS = ("mid_dispute_manual", "mid_incident_manual", "mid_codebook")

RIGHTS: dict[str, Any] = {
    "licence": "COW terms of use (not an open licence): cite the data; users cannot charge for access",
    "terms_url": "https://correlatesofwar.org/faq/",
    "open_licence": False,
    "attribution": "Correlates of War Project, Militarized Interstate Dispute data v5.0 (disputes) and v5.01 "
    "(incidents). Palmer, McManus, D'Orazio, Kenwick, Karstens, Bloch, Dietrich, Kahn, Ritter & Soules (2022), "
    "The MID5 Dataset, 2011-2014, Conflict Management and Peace Science 39(4)",
}
#: Dispute Coding Manual definitions per outcome code (relations_datasets.yaml definitions keys).
OUTCOME_TERM = {
    1: "outcome.victory",
    2: "outcome.victory",
    3: "outcome.yield",
    4: "outcome.yield",
    5: "outcome.stalemate",
    6: "outcome.compromise",
    7: "outcome.released",
    8: "outcome.unclear",
    9: "outcome",
}


def zip_url(key: str) -> str:
    return BASE + ZIPS[key][0]


def _int(v: Any) -> int | None:
    s = str(v or "").strip()
    if not s or not s.lstrip("-").isdigit():
        return None
    return int(s)


def unpack(key: str, body: bytes, *, strict: bool = True) -> dict[str, Any]:
    """CSV members -> rows (checked), document members -> bytes."""
    from reva.adapters.http.archive import read_zip_members

    _zip, members = ZIPS[key]
    got = read_zip_members(body, tuple(m.lower() for m, _ in members.values()), max_total=64 * 1024 * 1024)
    by_name = {k.rsplit("/", 1)[-1].lower(): v for k, v in got.items()}
    out: dict[str, Any] = {}
    for mk, (fname, min_rows) in members.items():
        data = by_name.get(fname.lower())
        if data is None:
            raise cd.drift(f"MID {ZIPS[key][0]}: member {fname} missing")
        if fname.endswith(".pdf"):
            out[mk] = data
            continue
        rows = cd.read_csv_bytes(data, name=fname, required=REQUIRED[mk])
        if strict and len(rows) < min_rows:
            raise cd.drift(f"MID {fname}: {len(rows)} rows < expected {min_rows} (truncated?)")
        if {r["version"] for r in rows} != {"5"}:
            raise cd.drift(f"MID {fname}: unexpected version column {sorted({r['version'] for r in rows})}")
        out[mk] = rows
    return out


def _date(r: Mapping[str, str], pre: str) -> tuple[str, int] | None:
    return cd.coded_date(r.get(f"{pre}year"), r.get(f"{pre}mon"), r.get(f"{pre}day"), missing=("", "-9", "-8", "-7"))


def _when(t: tuple[str, int] | None, fallback_year: int | None) -> date:
    if t:
        return date.fromisoformat(t[0])
    return date(fallback_year or 1900, 7, 1)


def kg_and_claims(
    tabs: Mapping[str, Sequence[Mapping[str, str]]],
    *,
    shas: Mapping[str, str],
    resolver: StateEntityResolver,
) -> dict[str, Any]:
    fa, fb = ZIPS["mid5"][1]["mida"][0], ZIPS["mid5"][1]["midb"][0]
    fi, fip = ZIPS["mid501"][1]["midi"][0], ZIPS["mid501"][1]["midip"][0]
    s5, s501 = shas["mid5"], shas["mid501"]
    ents: dict[str, KgEntity] = {}
    sts: list[KgStatement] = []
    claims: list[cd.ClaimRecord] = []

    def add(st: KgStatement | None) -> None:
        if st is not None:
            sts.append(st)

    parts: dict[str, list[tuple[int, Mapping[str, str]]]] = defaultdict(list)
    for i, r in enumerate(tabs["midb"]):
        parts[r["dispnum"]].append((i + 2, r))
    for i, r in enumerate(tabs["mida"]):
        n = int(r["dispnum"])
        line = i + 2
        did = f"mid:dispute:{n}"
        sel = {"file": fa, "row": line, "dispnum": n}
        start, end = _date(r, "st"), _date(r, "end")
        sides: dict[str, list[str]] = {"A": [], "B": []}
        for _ln, p in parts.get(r["dispnum"], []):
            s = "A" if p["sidea"] == "1" else "B"
            if p["stabb"] not in sides[s]:
                sides[s].append(p["stabb"])
        label = f"MID {n}: {', '.join(sides['A']) or '?'} vs {', '.join(sides['B']) or '?'} ({r['styear']})"
        ents[did] = KgEntity(
            qid=did,
            kind="militarized_interstate_dispute",
            labels={"en": label},
            descriptions={"en": f"Militarized interstate dispute {n}, COW MID {MID_VERSION} (MIDA)"},
        )
        add(
            cd.string_stmt(
                f"d{n}:event_type",
                did,
                "causdata:event_type",
                "militarized_interstate_dispute",
                raw_sha256=s5,
                selector=sel,
            )
        )
        for st in cd.event_times(
            did,
            f"d{n}",
            start=start,
            end=end,
            raw_sha256=s5,
            selector={**sel, "columns": ["stday", "stmon", "styear", "endday", "endmon", "endyear"]},
            qualifiers={
                "definition": "start = start date of the first militarized incident; end "
                "date per the Dispute Coding Manual termination rules"
            },
        ):
            add(st)
        for pid, var, col in (
            ("mid:hostility_level", "HostLev", "hostlev"),
            ("mid:highest_action", "HiAct", "hiact"),
            ("mid:fatality_level", "Fatality", "fatality"),
            ("mid:reciprocated", "Recip", "recip"),
        ):
            add(
                cd.coded_stmt(
                    f"d{n}:{col}", did, pid, "mid5", var, r[col], raw_sha256=s5, selector={**sel, "column": col}
                )
            )
        # participants and coded issues (MIDB)
        seen: dict[tuple[str, str], int] = defaultdict(int)
        for ln, p in parts.get(r["dispnum"], []):
            code = _int(p["ccode"])
            if code is None:
                continue
            side = "A" if p["sidea"] == "1" else "B"
            k = seen[(p["ccode"], side)]
            seen[(p["ccode"], side)] += 1
            pk = f"d{n}:p:{code}:{side}:{k}"
            ps, pe = _date(p, "st"), _date(p, "end")
            obj = resolver.entity("cow", code, _when(ps, _int(p["styear"])), provider_label=p["stabb"])
            psel = {"file": fb, "row": ln, "dispnum": n, "ccode": code}
            q: dict[str, Any] = {
                "role": "dispute participant",
                "side": side,
                "originator": p["orig"] == "1",
                "revisionist": p["revstate"] == "1",
                "stabb": p["stabb"],
                "code": str(code),
                "hostility_level": cd.value_label("mid5", "HostLev", p["hostlev"]),
                "highest_action": cd.value_label("mid5", "HiAct", p["hiact"]),
            }
            sts.append(
                KgStatement(
                    key=pk,
                    subject=did,
                    property="P710",
                    object_qid=obj,
                    start=ps[0] if ps else None,
                    start_precision=ps[1] if ps else None,
                    end=pe[0] if pe else None,
                    end_precision=pe[1] if pe else None,
                    qualifiers={k2: v for k2, v in q.items() if v is not None},
                    raw_sha256=s5,
                    selector=psel,
                )
            )
            if p["revstate"] == "1":
                for rank, col in ((1, "revtype1"), (2, "revtype2")):
                    c = _int(p[col])
                    if c is None or c not in (1, 2, 3, 4):
                        continue
                    add(
                        cd.coded_stmt(
                            f"{pk}:issue{rank}",
                            did,
                            "causdata:coded_issue",
                            "mid5",
                            "RevType",
                            c,
                            raw_sha256=s5,
                            selector={**psel, "column": col},
                            rank=rank,
                            sought_by=obj,
                            stabb=p["stabb"],
                            side=side,
                            note="revision type sought by a revisionist state: a coded issue, not a cause",
                        )
                    )
        # outcome
        oc = _int(r["outcome"])
        olabel = cd.value_label("mid5", "Outcome", r["outcome"])
        if oc is not None and oc != -9 and olabel:
            oid = f"{did}:outcome"
            ents[oid] = KgEntity(
                qid=oid,
                kind="dispute_outcome",
                labels={"en": f"Outcome of MID {n}: {olabel}"},
                descriptions={
                    "en": f"Outcome and settlement of militarized interstate dispute {n} "
                    f"(COW MID {MID_VERSION}, MIDA Outcome/Settle)"
                },
            )
            add(
                cd.string_stmt(
                    f"d{n}:o:event_type", oid, "causdata:event_type", "dispute_outcome", raw_sha256=s5, selector=sel
                )
            )
            for st in cd.event_times(
                oid,
                f"d{n}:o",
                point=end,
                raw_sha256=s5,
                selector={**sel, "columns": ["endday", "endmon", "endyear"]},
                qualifiers={"definition": "dispute end date"},
            ):
                add(st)
            add(
                cd.coded_stmt(
                    f"d{n}:o:outcome",
                    oid,
                    "mid:outcome",
                    "mid5",
                    "Outcome",
                    r["outcome"],
                    raw_sha256=s5,
                    selector={**sel, "column": "outcome"},
                )
            )
            add(
                cd.coded_stmt(
                    f"d{n}:o:settle",
                    oid,
                    "mid:settlement",
                    "mid5",
                    "Settle",
                    r["settle"],
                    raw_sha256=s5,
                    selector={**sel, "column": "settle"},
                )
            )
            claims.append(
                cd.ClaimRecord(
                    cause_id=did,
                    effect_id=oid,
                    relation="coded_outcome",
                    selector={
                        "file": fa,
                        "row": line,
                        "dispnum": n,
                        "columns": ["outcome", "settle", "endday", "endmon", "endyear"],
                    },
                    raw_sha256=s5,
                    codebook_term=OUTCOME_TERM[oc],
                    source_claim_id=f"{n}:outcome",
                )
            )
    # incidents (5.01)
    disputes = {r["dispnum"] for r in tabs["mida"]}
    iparts: dict[str, list[tuple[int, Mapping[str, str]]]] = defaultdict(list)
    for i, r in enumerate(tabs["midip"]):
        iparts[r["ord_incidnum"]].append((i + 2, r))
    by_dispute: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for i, r in enumerate(tabs["midi"]):
        o = int(r["ord_incidnum"])
        iid = f"mid:incident:{o}"
        line = i + 2
        sel = {"file": fi, "row": line, "ord_incidnum": o}
        if r["dispnum"] not in disputes:
            raise cd.drift(f"MIDI row {line}: dispute {r['dispnum']} not in MIDA")
        by_dispute[r["dispnum"]].append((o, line))
        act = cd.value_label("mid5", "HiAct", r["action"]) or "action not coded"
        ents[iid] = KgEntity(
            qid=iid,
            kind="militarized_incident",
            labels={"en": f"MID incident {o}: {act} ({r['styear']})"},
            descriptions={
                "en": f"Militarized interstate incident {o} (v5.01 order; 5.0 number "
                f"{r['incidnum']}) of dispute {r['dispnum']}, COW MIDI 5.01"
            },
        )
        add(
            cd.string_stmt(
                f"i{o}:event_type", iid, "causdata:event_type", "militarized_incident", raw_sha256=s501, selector=sel
            )
        )
        for st in cd.event_times(
            iid,
            f"i{o}",
            start=_date(r, "st"),
            end=_date(r, "end"),
            raw_sha256=s501,
            selector={**sel, "columns": ["stday", "stmon", "styear", "endday", "endmon", "endyear"]},
        ):
            add(st)
        sts.append(
            KgStatement(
                key=f"i{o}:part_of",
                subject=iid,
                property="P361",
                object_qid=f"mid:dispute:{int(r['dispnum'])}",
                qualifiers={"variable": "DispNum"},
                raw_sha256=s501,
                selector=dict(sel),
            )
        )
        for pid, var, col in (
            ("mid:highest_action", "HiAct", "action"),
            ("mid:hostility_level", "HostLev", "hostlev"),
            ("mid:fatality_level", "Fatality", "fatality"),
        ):
            add(
                cd.coded_stmt(
                    f"i{o}:{col}", iid, pid, "mid5", var, r[col], raw_sha256=s501, selector={**sel, "column": col}
                )
            )
        iseen: dict[tuple[str, str], int] = defaultdict(int)
        for ln, p in iparts.get(r["ord_incidnum"], []):
            code = _int(p["ccode"])
            if code is None:
                continue
            side = "A" if p["insidea"] == "1" else "B"
            k = iseen[(p["ccode"], side)]
            iseen[(p["ccode"], side)] += 1
            obj = resolver.entity("cow", code, _when(_date(p, "st"), _int(p["styear"])), provider_label=p["stabb"])
            q = {
                "role": "incident participant",
                "incident_side": side,
                "dispute_side": "A" if p["sidea"] == "1" else "B",
                "stabb": p["stabb"],
                "code": str(code),
                "action": cd.value_label("mid5", "HiAct", p["action"]),
                "revision_type_1": cd.value_label("mid5", "RevType", p["revtype1"]),
                "revision_type_2": cd.value_label("mid5", "RevType", p["revtype2"]),
            }
            sts.append(
                KgStatement(
                    key=f"i{o}:p:{code}:{side}:{k}",
                    subject=iid,
                    property="P710",
                    object_qid=obj,
                    qualifiers={k2: v for k2, v in q.items() if v not in (None, "Not applicable")},
                    raw_sha256=s501,
                    selector={"file": fip, "row": ln, "ord_incidnum": o, "ccode": code},
                )
            )
    for dn, items in by_dispute.items():
        items.sort()
        for (prev, _pl), (cur, cl) in pairwise(items):
            sts.append(
                KgStatement(
                    key=f"i{cur}:follows",
                    subject=f"mid:incident:{cur}",
                    property="P155",
                    object_qid=f"mid:incident:{prev}",
                    qualifiers={
                        "basis": "MIDI 5.01 ord_incidnum: incidents re-ordered by start and end "
                        "dates within the dispute (temporal order, not a causal link)",
                        "dispnum": int(dn),
                    },
                    raw_sha256=s501,
                    selector={"file": fi, "row": cl, "ord_incidnum": cur},
                )
            )
    return {"entities": list(ents.values()), "statements": sts, "claims": claims}


# ----------------------------------------------------------------------------- atlas: mid5_cy (computed)

CY_METHODS = {
    "mid_disputes_onset": "count of distinct MIDs in which the state's participation spell starts in the year (MIDB StYear)",
    "mid_disputes_onset_side_a": "as mid_disputes_onset, the state on side A (the side of the first militarized action, "
    "a convention, not an initiator-as-cause coding)",
    "mid_disputes_onset_revisionist": "as mid_disputes_onset, the state coded revisionist (RevState = 1)",
    "mid_disputes_onset_use_of_force": "as mid_disputes_onset, the state's own hostility level >= 4 (use of force or war)",
    "mid_disputes_active": "count of distinct MIDs with a participation spell of the state overlapping the year "
    "(StYear..EndYear)",
}


def mid5_cy_rows(
    midb: Sequence[Mapping[str, str]], *, raw_sha256: str, crosswalk: StateCodeCrosswalk
) -> list[dict[str, Any]]:
    members: dict[tuple[str, str, int, int], set[str]] = defaultdict(set)
    lines: dict[tuple[str, str, int, int], list[int]] = defaultdict(list)
    for i, p in enumerate(midb):
        code, y0, y1 = _int(p["ccode"]), _int(p["styear"]), _int(p["endyear"])
        if code is None or y0 is None or y0 < 0:
            continue
        inds = ["mid_disputes_onset"]
        if p["sidea"] == "1":
            inds.append("mid_disputes_onset_side_a")
        if p["revstate"] == "1":
            inds.append("mid_disputes_onset_revisionist")
        if (_int(p["hostlev"]) or 0) >= 4:
            inds.append("mid_disputes_onset_use_of_force")
        for ind in inds:
            members[(ind, "cow", code, y0)].add(p["dispnum"])
            lines[(ind, "cow", code, y0)].append(i + 2)
        for y in range(y0, max(y0, y1 if y1 is not None and y1 > 0 else y0) + 1):
            members[("mid_disputes_active", "cow", code, y)].add(p["dispnum"])
            lines[("mid_disputes_active", "cow", code, y)].append(i + 2)
    return cd.count_atlas_rows(
        members,
        lines,
        source=ATLAS_SOURCE,
        dataset="mid5_cy",
        crosswalk=crosswalk,
        file=ZIPS["mid5"][1]["midb"][0],
        raw_sha256=raw_sha256,
        methods=CY_METHODS,
        computed_from="COW MID 5.0 participant data (MIDB 5.0.csv)",
        geo_prefix="cow",
        id_label="dispnums",
    )


# ----------------------------------------------------------------------------- atlas source + ingest


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Correlates of War Project",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST, "cran.r-project.org"),
        datasets=("mid5_cy",),
        dataset_ids=("cow-mid-5.0", "cow-mid-incidents-5.01", f"countrycode-{COUNTRYCODE_VERSION}"),
        cadence_seconds=90 * 86400,
        planned_requests=5,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "geo": "COW state numbers (provider_geo cow:<ccode>)",
            "time": "year of participation start / active years",
            "kg": "mid:dispute:<dispnum>[:outcome], mid:incident:<ord_incidnum>",
            "claims": "registry/causal/relations_datasets.yaml dataset mid5",
        },
        notes="Shares the cow.data source/budget with atlas.cow (A-HISTECON). Also fetches countrycode (CRAN).",
    )


def build(
    raws: Mapping[str, RawAcquisition],
    *,
    crosswalk: StateCodeCrosswalk,
    qid_for_iso3: Mapping[str, str],
    countrycode_sha256: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    shas = {k: hashlib.sha256(r.body).hexdigest() for k, r in raws.items()}
    tabs: dict[str, Any] = {}
    for k in ZIPS:
        tabs.update(unpack(k, raws[k].body, strict=strict))
    docs = {d: cd.check_document(d, tabs[d]) for d in DOCS}
    resolver = StateEntityResolver(crosswalk, qid_for_iso3)
    kg = kg_and_claims(tabs, shas=shas, resolver=resolver)
    stmts = kg["statements"]
    if countrycode_sha256:
        stmts = stmts + code_correspondence(resolver, raw_sha256=countrycode_sha256)
    cd.check_unique_keys(stmts)
    return {
        "shas": shas,
        "documents": docs,
        "mid5_cy": mid5_cy_rows(tabs["midb"], raw_sha256=shas["mid5"], crosswalk=crosswalk),
        "entities": kg["entities"] + resolver.code_entities(),
        "statements": stmts,
        "claims": kg["claims"],
        "row_counts": {k: len(v) for k, v in tabs.items() if isinstance(v, list)},
        "unresolved_state_codes": {k: sorted(v) for k, v in resolver.unresolved.items()},
    }


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the two MID zips + countrycode, archive, register ``cow.mid5_cy``, load KG records, deliver claims."""
    crosswalk, cc_sha = await fetch_countrycode(run)
    raws = {}
    for k in ZIPS:
        raws[k] = await run.get(zip_url(k), key=f"{k}:{MID_VERSION}", media_types=ZIP_MEDIA, max_bytes=MAX_BYTES)
    out = build(
        raws,
        crosswalk=crosswalk,
        qid_for_iso3=load_qid_for_iso3(),
        countrycode_sha256=cc_sha,
        strict=run.origin == "live",
    )
    for raw in raws.values():
        run.archive(raw)
    shas = out["shas"]
    res: dict[str, Any] = {
        "row_counts": out["row_counts"],
        "atlas": {},
        "codebooks": {k: {x: v[x] for x in ("sha256", "matches")} for k, v in out["documents"].items()},
    }
    if run.wants("mid5_cy"):
        r = run.register(
            dataset="mid5_cy",
            frame=atlas_frame(out["mid5_cy"]),
            raw_sha256s=[shas["mid5"]],
            rights=RIGHTS,
            coverage_note="COMPUTED from COW MID 5.0 participants (MIDB, 1816-2014): disputes per state and year "
            "(onset, onset on side A, onset as revisionist, onset with use of force, active); only state-years with "
            ">= 1 dispute are listed",
        )
        res["atlas"]["mid5_cy"] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    retrieved = max(r.retrieved_at for r in raws.values())
    if run.dataset is None and cd.kg_available(run.engine):
        res["kg"] = cd.load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision="mid-5.0-5.01",
            raw_sha256s=sorted(set(shas.values()) | {cc_sha}),
            retrieved_at=retrieved,
            origin=run.origin,
        )
    if run.dataset is None:
        if all(d["matches"] for d in out["documents"].values()):
            e = cd.dataset_entry("mid5")
            res["claims"] = cd.deliver_claims(
                run,
                [
                    cd.ClaimDelivery(
                        source_key=SOURCE_KEY,
                        dataset="mid5",
                        dataset_revision=e["dataset_revision"],
                        codebook=cd.codebook("mid5", {d: v["sha256"] for d, v in out["documents"].items()}),
                        raw_sha256s=[shas["mid5"]],
                        retrieved_at=retrieved,
                        origin=run.origin,
                        claims=out["claims"],
                        complete=True,
                    )
                ],
            )
        else:
            res["claims_withheld"] = {"mid5": "a codebook / coding manual PDF differs from the pinned sha256"}
    res["unresolved_state_codes"] = {
        k: v[:3] + (["..."] if len(v) > 3 else []) for k, v in out["unresolved_state_codes"].items()
    }
    return res
