"""UCDP Conflict Termination (v4-2024) and Peace Agreements (22.2) (agent CAUSDATA).

Source and access
-----------------
``ucdp.uu.se/downloads`` (no token, no account; CC BY 4.0 -- "All datasets are free of charge and licensed under
CC BY 4.0"), registered source ``ucdp.downloads`` (KGGEOPOL), one request per file:

* ``UCDPConflictTerminationDataset_v4_2024_Conflict.csv`` -- Kreutz (2010) conflict-level termination data, one row per
  conflict-year 1946-2024 (compatible with UCDP/PRIO ACD 25.1): conflict episodes (``c_epid``), whether the episode
  ends that year (``c_epterm``), how it ended (``c_outcome`` 1-6) and when (``c_ependdate`` with precision).
* ``ucdp-peace-agreements-222.xlsx`` -- peace agreements 1975-2021 (signature date, conflict ids, type, provisions).
* Both codebook PDFs; their sha256 must equal ``registry/causal/relations_datasets.yaml`` before claims are delivered.

Outputs (semantics and reasons: ``registry/causal/relations_datasets.yaml`` datasets ``term_conflict``,
``peace_agreements``)
-------
* KG events: ``ucdp:episode:<c_epid>`` (armed_conflict_episode, ``P361`` part of KGGEOPOL's ``ucdp:conflict:<id>``,
  ``P155`` follows the previous episode, incompatibility as ``causdata:coded_issue`` -- an issue, not a cause),
  ``ucdp:episode:<c_epid>:termination`` (conflict_termination), ``ucdp:pa:<paid>`` (peace_agreement).
* T1 claims: ``coded_outcome`` episode -> termination (one per terminated episode, codebook term
  ``c_outcome=<value>``). Peace agreements give **no** claim (connector 1.1.0, audit M6): the codebook relates an
  agreement to "conflicts related to the dyads signing the peace agreement" (``conflict_id``) -- a relatedness link,
  stored as the context statement ``ucdppa:conflict``, never as ``reaction_to``. The ``peace_agreements`` delivery
  stays (empty, complete) so the claims of 1.0.0 are retracted through the loader.
* Context only (never a claim): ``causdata:agreement_in_termination_window`` termination -> agreement(s) of the same
  conflict signed in the last active year or the first inactive year when the termination outcome is 1 (peace
  agreement): REVA's pairing of two datasets, with the rule in the qualifiers.
* Atlas tables ``ucdp.term_cy`` (COMPUTED: episodes ended per location and year by outcome) and ``ucdp.pa_cy``
  (COMPUTED: peace agreements signed per location and year).

FIXB (1.2.x, second causal audit ``docs/dev/review_v3_causal_audit2.md``): every terminal row of an episode is kept
(N6, ``…:termination:<year>`` per row, differing outcomes contest each other); UCDP precision code 6 is year precision
with the typed note ``source_date_certainty`` and 7 a typed missing date (N10, ``causdata_common.ucdp_date``); the
claims carry ``codebook.quote_source`` (archived PDF sha256 + page, N8); the KG source revision names the version.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors.atlas_geopol_common import (
    COUNTRYCODE_VERSION,
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    atlas_frame,
    fetch_countrycode,
)
from reva.ports.connectors import RawAcquisition

SOURCE_KEY = "ucdp.downloads"
CONNECTOR_NAME = "atlas.ucdp_term"
CONNECTOR_VERSION = "1.2.1"  # 1.1.0 (AUDFIX M6): peace agreements are context, not reaction_to claims; 1.2.0 (FIXB,
#: audit2 N6/N8/N10): every terminal row kept, codebook quote_source, UCDP precision 6 (assigned) / 7 (missing); 1.2.1:
#: superseded single-terminal statements retracted by their stored keys (1.2.0 was registered, never run)
# 1.1.1: ucdppa:conflict statements keep their 1.0.0 content (the context reading lives in the relation registry)
PARSER_VERSION = "atlas-ucdp-term-parse-1"
ATLAS_SOURCE = "ucdp"
HOST = "ucdp.uu.se"
BASE = f"https://{HOST}/downloads/"
TERM_VERSION = "4.2024002"
TERM_FILE = "UCDPConflictTerminationDataset_v4_2024_Conflict.csv"
PA_VERSION = "22.2"
PA_FILE = "ucdp-peace-agreements-222.xlsx"
URLS = {"term": BASE + "monadterm/" + TERM_FILE, "pa": BASE + "peace/" + PA_FILE}
MEDIA = {
    "term": ("text/csv", "application/csv", "application/octet-stream", "text/plain"),
    "pa": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
        "application/zip",
    ),
}
PDF_MEDIA = ("application/pdf", "application/octet-stream")
CODEBOOKS = {"term_conflict": "term_codebook", "peace_agreements": "pa_codebook"}
MAX_BYTES = 8_000_000
TERM_REQUIRED = (
    "conflict_id",
    "location",
    "incompatibility",
    "territory_name",
    "year",
    "start_date",
    "start_prec",
    "start_date2",
    "start_prec2",
    "gwno_loc",
    "c_epid",
    "c_epno",
    "c_ep_startyear",
    "c_epterm",
    "c_outcome",
    "c_ep_endyear",
    "c_ependdate",
    "c_ependprec",
    "version",
)
PA_REQUIRED = (
    "paid",
    "gwno",
    "conflict_id",
    "incompatibility",
    "pa_name",
    "year",
    "pa_date",
    "ended",
    "duration",
    "cease",
    "inclusive",
    "pa_type",
    "version",
)
#: UCDP date precision: ``causdata_common.ucdp_date`` (6 = year assigned, typed note; 7 = typed missing; N10).
OUTCOME_SLUG = {
    1: "peace_agreement",
    2: "ceasefire",
    3: "victory_government",
    4: "victory_rebels",
    5: "low_activity",
    6: "actor_ceased",
}
INCOMPAT = {"1": "Territory", "2": "Government", "3": "Government and territory"}

RIGHTS: dict[str, str] = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "Uppsala Conflict Data Program (UCDP), Uppsala University: Conflict Termination Dataset v4-2024 "
    "(Kreutz 2010, JPR 47(2)) and Peace Agreement Dataset 22.2 (Pettersson, Högbladh & Öberg 2019, JPR 56(4))",
}


def _codes(field: Any) -> list[int]:
    out = []
    for part in str(field or "").split(","):
        s = part.strip()
        if s.lstrip("-").isdigit():
            out.append(int(s))
    return out


def parse_term(body: bytes, *, strict: bool = True) -> list[dict[str, str]]:
    rows = cd.read_csv_bytes(body, name=TERM_FILE, required=TERM_REQUIRED)
    if strict and len(rows) < 2500:
        raise cd.drift(f"{TERM_FILE}: {len(rows)} rows < 2500 (truncated?)")
    if {r["version"] for r in rows} != {TERM_VERSION}:
        raise cd.drift(f"{TERM_FILE}: unexpected version {sorted({r['version'] for r in rows})}")
    return rows


def parse_pa(body: bytes, *, strict: bool = True) -> list[dict[str, str]]:
    from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError

    try:
        wb = Workbook(body)
        table = wb.table("Dataset")
    except (XlsxError, KeyError) as e:
        raise cd.drift(f"{PA_FILE}: unreadable workbook or no 'Dataset' sheet") from e
    header = [str(h or "").strip() for h in table[0]] if table else []
    missing = [c for c in PA_REQUIRED if c not in header]
    if missing:
        raise cd.drift(f"{PA_FILE}: missing columns {missing}")
    rows = []
    for i, r in enumerate(table[1:]):
        if not any(v not in (None, "") for v in r):
            continue
        d = {h: ("" if v is None else str(v).strip()) for h, v in zip(header, r, strict=False) if h}
        d["_row"] = str(i + 2)
        rows.append(d)
    if strict and len(rows) < 350:
        raise cd.drift(f"{PA_FILE}: {len(rows)} rows < 350 (truncated?)")
    if {r["version"] for r in rows} != {PA_VERSION}:
        raise cd.drift(f"{PA_FILE}: unexpected version {sorted({r['version'] for r in rows})}")
    return rows


def _yes(v: Any) -> bool | None:
    s = str(v or "").strip().lower()
    if s in ("1", "true", "yes"):
        return True
    if s in ("0", "false", "no"):
        return False
    return None


def episodes(rows: Sequence[Mapping[str, str]]) -> dict[str, dict[str, Any]]:
    """c_epid -> {rows [(line, row)], terminal_rows [(line, row)], terminal (the last; kept for callers), terminals}."""
    eps: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(rows):
        e = eps.setdefault(r["c_epid"], {"rows": [], "terminal": None, "terminals": 0})
        e["rows"].append((i + 2, r))
    for e in eps.values():
        e["rows"].sort(key=lambda x: int(x[1]["year"]))
        terms = [x for x in e["rows"] if x[1]["c_epterm"] == "1"]
        e["terminals"] = len(terms)
        e["terminal_rows"] = terms  # every terminal row (audit2 N6: none is chosen)
        e["terminal"] = terms[-1] if terms else None
    return eps


def kg_and_claims(
    term: Sequence[Mapping[str, str]], pa: Sequence[Mapping[str, str]], *, shas: Mapping[str, str]
) -> dict[str, Any]:
    st_, sp = shas["term"], shas["pa"]
    ents: dict[str, KgEntity] = {}
    sts: list[KgStatement] = []
    claims: dict[str, list[cd.ClaimRecord]] = {"term_conflict": [], "peace_agreements": []}
    anomalies: dict[str, Any] = {}

    def add(s: KgStatement | None) -> None:
        if s is not None:
            sts.append(s)

    # peace agreements first (the termination pairing needs them)
    pa_by_conflict: dict[int, list[tuple[int, str]]] = defaultdict(list)  # cid -> [(year, paid)]
    for r in pa:
        paid = int(r["paid"])
        line = int(r["_row"])
        pid = f"ucdp:pa:{paid}"
        sel = {"file": PA_FILE, "sheet": "Dataset", "row": line, "paid": paid}
        pa_iso = cd.iso_date(r["pa_date"])
        when = (pa_iso, 11) if pa_iso else cd.coded_date(r["year"])
        ents[pid] = KgEntity(
            qid=pid,
            kind="peace_agreement",
            labels={"en": r["pa_name"] or f"UCDP peace agreement {paid}"},
            descriptions={
                "en": f"Peace agreement {paid} (UCDP Peace Agreement Dataset {PA_VERSION}), "
                f"signed {r['pa_date'] or r['year']}"
            },
        )
        add(
            cd.string_stmt(
                f"pa{paid}:event_type", pid, "causdata:event_type", "peace_agreement", raw_sha256=sp, selector=sel
            )
        )
        for s in cd.event_times(
            pid,
            f"pa{paid}",
            point=when,
            raw_sha256=sp,
            selector={**sel, "column": "pa_date"},
            qualifiers={"definition": "The date of the last signature of the peace agreement."},
        ):
            add(s)
        add(
            cd.coded_stmt(
                f"pa{paid}:type",
                pid,
                "ucdppa:agreement_type",
                "peace_agreements",
                "pa_type",
                r["pa_type"],
                raw_sha256=sp,
                selector={**sel, "column": "pa_type"},
            )
        )
        add(
            cd.coded_stmt(
                f"pa{paid}:inclusive",
                pid,
                "ucdppa:inclusive",
                "peace_agreements",
                "inclusive",
                r["inclusive"],
                raw_sha256=sp,
                selector={**sel, "column": "inclusive"},
            )
        )
        cease = _yes(r["cease"])
        if cease is not None:
            add(
                cd.string_stmt(
                    f"pa{paid}:cease",
                    pid,
                    "ucdppa:ceasefire_provision",
                    "Yes" if cease else "No",
                    raw_sha256=sp,
                    selector={**sel, "column": "cease"},
                    code=r["cease"],
                    variable="cease",
                )
            )
        if r["incompatibility"] in INCOMPAT:
            add(
                cd.string_stmt(
                    f"pa{paid}:issue",
                    pid,
                    "causdata:coded_issue",
                    INCOMPAT[r["incompatibility"]],
                    raw_sha256=sp,
                    selector={**sel, "column": "incompatibility"},
                    variable="incompatibility",
                    code=r["incompatibility"],
                    note="incompatibility addressed by the agreement: a coded issue",
                )
            )
        if _yes(r["ended"]) and cd.iso_date(r["duration"]):
            add(
                KgStatement(
                    key=f"pa{paid}:ended_on",
                    subject=pid,
                    property="ucdppa:ended_on",
                    value=cd.time_value(cd.iso_date(r["duration"]), 11),  # type: ignore[arg-type]
                    qualifiers={"variable": "duration", "ended": True},
                    raw_sha256=sp,
                    selector={**sel, "column": "duration"},
                )
            )
        year = int(r["year"]) if r["year"].isdigit() else None
        for cid in _codes(r["conflict_id"]):
            sts.append(
                KgStatement(
                    key=f"pa{paid}:conflict:{cid}",
                    subject=pid,
                    property="ucdppa:conflict",
                    object_qid=f"ucdp:conflict:{cid}",
                    qualifiers={"variable": "conflict_id"},
                    raw_sha256=sp,
                    selector={**sel, "column": "conflict_id"},
                )
            )
            if year is not None:
                pa_by_conflict[cid].append((year, f"ucdp:pa:{paid}"))

    eps = episodes(term)
    dup = {k: e["terminals"] for k, e in eps.items() if e["terminals"] > 1}
    if dup:
        anomalies["episodes_with_several_terminal_rows"] = dup
    # Start of each conflict (start_date / start_prec, replicated by the termination data from the UCDP/PRIO ACD):
    # P580 on KGGEOPOL's ucdp:conflict:<id>, the date the causal temporal check reads for reaction_to claims.
    first_rows: dict[str, tuple[int, Mapping[str, str]]] = {}
    for i, r in enumerate(term):
        cur = first_rows.get(r["conflict_id"])
        if cur is None or int(r["year"]) < int(cur[1]["year"]):
            first_rows[r["conflict_id"]] = (i + 2, r)
    for cid_s, (ln, r) in sorted(first_rows.items(), key=lambda x: int(x[0])):
        ud = cd.ucdp_date(r["start_date"], r["start_prec"], which="start")
        csel = {"file": TERM_FILE, "row": ln, "conflict_id": int(cid_s), "columns": ["start_date", "start_prec"]}
        cq = {
            "definition": "conflict start date (UCDP/PRIO ACD start_date: date of the first battle-related "
            "death), as replicated in the Conflict Termination Dataset",
            "ucdp_start_prec": r["start_prec"],
            **ud.qualifiers,
        }
        if ud.missing is not None:  # precision 7: the coded value is a placeholder (typed missing, audit2 N10)
            add(cd.missing_time_statement(f"ucdp:conflict:{int(cid_s)}", f"conf{int(cid_s)}:start", "P580",
                                          ud.missing, raw_sha256=st_, selector=csel, qualifiers=cq))  # fmt: skip
        for s in cd.event_times(
            f"ucdp:conflict:{int(cid_s)}",
            f"conf{int(cid_s)}",
            start=ud.value,
            raw_sha256=st_,
            selector=csel,
            qualifiers=cq,
        ):
            add(s)
    prev_ep: dict[str, tuple[int, str]] = {}
    for epid, e in sorted(
        eps.items(), key=lambda x: (int(x[1]["rows"][0][1]["conflict_id"]), int(x[1]["rows"][0][1]["c_epno"] or 0))
    ):
        fl, first = e["rows"][0]
        cid = int(first["conflict_id"])
        eid = f"ucdp:episode:{epid}"
        sel = {"file": TERM_FILE, "row": fl, "c_epid": epid}
        incompat = INCOMPAT.get(first["incompatibility"], "")
        what = first["territory_name"] if first["incompatibility"] == "1" and first["territory_name"] else incompat
        label = f"{first['location']}: {what} - episode {first['c_epno']} (UCDP conflict {cid})"
        years = [int(r["year"]) for _, r in e["rows"]]
        ents[eid] = KgEntity(
            qid=eid,
            kind="armed_conflict_episode",
            labels={"en": label},
            descriptions={
                "en": f"Episode {first['c_epno']} of UCDP armed conflict {cid}, active "
                f"{min(years)}-{max(years)} (UCDP Conflict Termination Dataset v4-2024)"
            },
        )
        add(
            cd.string_stmt(
                f"ep{epid}:event_type",
                eid,
                "causdata:event_type",
                "armed_conflict_episode",
                raw_sha256=st_,
                selector=sel,
            )
        )
        sts.append(
            KgStatement(
                key=f"ep{epid}:part_of",
                subject=eid,
                property="P361",
                object_qid=f"ucdp:conflict:{cid}",
                qualifiers={"variable": "conflict_id"},
                raw_sha256=st_,
                selector=dict(sel),
            )
        )
        sd = cd.ucdp_date(first["start_date2"], first["start_prec2"], which="start")
        start, start_q = sd.value, dict(sd.qualifiers)
        if start is None:
            start = cd.coded_date(first["c_ep_startyear"])
            if sd.missing is not None and start is not None:
                start_q["start_date2_missing"] = dict(sd.missing)
                start_q["note"] = (
                    "start_date2 is the codebook's placeholder (start_prec2 = 7): the start year is the episode's "
                    "first active year (c_ep_startyear)"
                )
        terms = e["terminal_rows"]
        multi = len(terms) > 1
        ends = [cd.ucdp_date(tr_["c_ependdate"], tr_["c_ependprec"], which="end") for _, tr_ in terms]
        # an ordinary episode (one terminal row, precision codes 1-5) keeps exactly the 1.1.x time statements
        plain = not multi and not start_q and not any(x.qualifiers or x.missing for x in ends)
        for s in cd.event_times(
            eid,
            f"ep{epid}",
            start=start,
            end=ends[0].value if plain and ends else None,
            raw_sha256=st_,
            selector={**sel, "columns": ["start_date2", "start_prec2", "c_ependdate", "c_ependprec"]},
            qualifiers={
                "definition": "start = episode start (start_date2, first year of the episode); "
                "end = c_ependdate (termination)",
                **start_q,
            },
        ):
            add(s)
        if first["incompatibility"] in INCOMPAT:
            add(
                cd.string_stmt(
                    f"ep{epid}:issue",
                    eid,
                    "causdata:coded_issue",
                    incompat,
                    raw_sha256=st_,
                    selector={**sel, "column": "incompatibility"},
                    variable="incompatibility",
                    code=first["incompatibility"],
                    territory=first["territory_name"] or None,
                    note="UCDP incompatibility: the contested issue, not a cause",
                )
            )
        if str(cid) in prev_ep:
            sts.append(
                KgStatement(
                    key=f"ep{epid}:follows",
                    subject=eid,
                    property="P155",
                    object_qid=f"ucdp:episode:{prev_ep[str(cid)][1]}",
                    qualifiers={"basis": "c_epno order within the conflict (not a causal link)"},
                    raw_sha256=st_,
                    selector=dict(sel),
                )
            )
        prev_ep[str(cid)] = (int(first["c_epno"] or 0), epid)
        # Every terminal row the source codes is kept (audit2 N6): one termination record and one claim per row when
        # the episode has several; claims whose coded outcomes differ contest each other.
        recs = []
        for (tl, tr), ed in zip(terms, ends, strict=True):
            oc = int(tr["c_outcome"]) if tr["c_outcome"].isdigit() else None
            recs.append((tl, tr, oc, cd.value_label("term_conflict", "c_outcome", oc), ed))
        dup_note: dict[str, Any] | None = None
        if multi:
            dup_note = {
                "flag": "source_duplicate_terminal_rows",
                "terminal_rows": [
                    {"row": tl, "year": int(tr["year"]), "c_outcome": tr["c_outcome"],
                     "c_ependdate": tr["c_ependdate"], "c_ependprec": tr["c_ependprec"]}
                    for tl, tr, _, _, _ in recs
                ],
                "outcomes_differ": len({oc for _, _, oc, _, _ in recs}) > 1,
                "note": "UCDP codes this conflict episode with several terminal rows (c_epterm = 1); REVA keeps every "
                "one as the source states and chooses none",
            }  # fmt: skip
            add(
                cd.string_stmt(
                    f"ep{epid}:dup_terminal",
                    eid,
                    "ucdpterm:source_duplicate_terminal_rows",
                    f"{len(recs)} terminal rows coded by UCDP: "
                    + "; ".join(
                        f"{tr['year']} (c_outcome {tr['c_outcome']}: {lab or 'no outcome'})"
                        for _, tr, _, lab, _ in recs
                    ),
                    raw_sha256=st_,
                    selector={**sel, "rows": [tl for tl, _, _, _, _ in recs], "column": "c_epterm"},
                    outcomes_differ=dup_note["outcomes_differ"],
                )
            )
            ents[f"{eid}:termination"] = KgEntity(
                qid=f"{eid}:termination",
                kind="superseded_record",
                labels={
                    "en": f"Superseded record: termination of UCDP conflict episode {epid} (see its per-row records)"
                },
                descriptions={
                    "en": "atlas.ucdp_term up to 1.1.1 kept only the last of the episode's terminal rows here; from "
                    "1.2.0 every terminal row has its own record ucdp:episode:<c_epid>:termination:<year>"
                },
            )
        keys = {tr["year"]: f"{epid}:{tr['year']}:c_outcome" for _, tr, _, _, _ in recs}
        for tl, tr, oc, olabel, ed in recs:
            year = tr["year"]
            tid = f"{eid}:termination:{year}" if multi else f"{eid}:termination"
            tp = f"ep{epid}:t{year}" if multi else f"ep{epid}:t"
            tsel = {"file": TERM_FILE, "row": tl, "c_epid": epid}
            tcols = {**tsel, "columns": ["c_ependdate", "c_ependprec"]}
            tq: dict[str, Any] = {**dict(ed.qualifiers)}
            if dup_note is not None:
                tq["source_duplicate_terminal_rows"] = dup_note
            if not plain:  # the episode's end, per terminal row, with its typed note (or typed missing)
                eq = {"definition": "end = c_ependdate (termination)", **tq}
                ekey = f"ep{epid}:t{year}" if multi else f"ep{epid}"
                if ed.value is not None:
                    for s in cd.event_times(eid, ekey, end=ed.value, raw_sha256=st_, selector=tcols, qualifiers=eq):
                        add(s)
                elif ed.missing is not None:
                    add(cd.missing_time_statement(eid, f"{ekey}:end", "P582", ed.missing, raw_sha256=st_,
                                                  selector=tcols, qualifiers=eq))  # fmt: skip
            if oc is None or olabel is None:
                anomalies.setdefault("terminated_without_outcome", []).append(epid)
                continue
            ents[tid] = KgEntity(
                qid=tid,
                kind="conflict_termination",
                labels={"en": f"End of {label}: {olabel}" + (f" (terminal row {year})" if multi else "")},
                descriptions={
                    "en": f"Termination of conflict episode {epid} coded as outcome {oc} "
                    "(UCDP Conflict Termination Dataset v4-2024, c_outcome)"
                    + (f"; the source codes {len(recs)} terminal rows for this episode, all kept" if multi else "")
                },
            )
            add(
                cd.string_stmt(
                    f"{tp}:event_type",
                    tid,
                    "causdata:event_type",
                    "conflict_termination",
                    raw_sha256=st_,
                    selector=tsel,
                )
            )
            tdef = {
                "definition": "The date, as precise as possible, when the conflict is terminated.",
                "ucdp_ependprec": tr["c_ependprec"],
                **tq,
            }
            if ed.value is not None:
                for s in cd.event_times(tid, tp, point=ed.value, raw_sha256=st_, selector=tcols, qualifiers=tdef):
                    add(s)
            elif ed.missing is not None:
                add(cd.missing_time_statement(tid, f"{tp}:point", "P585", ed.missing, raw_sha256=st_, selector=tcols,
                                              qualifiers=tdef))  # fmt: skip
            add(
                cd.coded_stmt(
                    f"{tp}:outcome",
                    tid,
                    "ucdpterm:outcome",
                    "term_conflict",
                    "c_outcome",
                    oc,
                    raw_sha256=st_,
                    selector={**tsel, "column": "c_outcome"},
                )
            )
            scope: dict[str, Any] = {}
            contests: tuple[str, ...] = ()
            if dup_note is not None:
                scope["source_duplicate_terminal_rows"] = dup_note
                if dup_note["outcomes_differ"]:
                    scope["contested_by_source"] = True
                    contests = tuple(
                        f"ds:{SOURCE_KEY}:term_conflict:{keys[tr2['year']]}"
                        for _, tr2, o2, _, _ in recs
                        if tr2 is not tr and o2 != oc
                    )
            if ed.missing is not None:
                scope["effect_time"] = dict(ed.missing)
            elif "source_date_certainty" in tq:
                scope["effect_time_certainty"] = tq["source_date_certainty"]
            claims["term_conflict"].append(
                cd.ClaimRecord(
                    cause_id=eid,
                    effect_id=tid,
                    relation="coded_outcome",
                    selector={
                        "file": TERM_FILE,
                        "row": tl,
                        "c_epid": epid,
                        "columns": ["c_outcome", "c_ependdate", "c_ependprec", "c_epterm"],
                    },
                    raw_sha256=st_,
                    codebook_term=f"c_outcome={oc}",
                    source_claim_id=keys[year] if multi else f"{epid}:c_outcome",
                    scope=scope,
                    contests=contests,
                )
            )
            if oc == 1:
                ey = int(tr["c_ep_endyear"] or tr["year"])
                cands = sorted({p for y, p in pa_by_conflict.get(cid, []) if y in (ey, ey + 1)})
                for p in cands:
                    sts.append(
                        KgStatement(
                            key=f"{tp}:pa:{p.rsplit(':', 1)[1]}",
                            subject=tid,
                            property="causdata:agreement_in_termination_window",
                            object_qid=p,
                            qualifiers={
                                "rule": "same conflict_id; agreement year = last active year or first inactive year "
                                "(termination codebook, outcome 1); REVA pairing, not a causal claim",
                                "candidates": len(cands),
                                "c_ep_endyear": ey,
                            },
                            raw_sha256=st_,
                            selector={**tsel, "columns": ["conflict_id", "c_ep_endyear", "c_outcome"]},
                        )
                    )
    return {"entities": list(ents.values()), "statements": sts, "claims": claims, "anomalies": anomalies}


# ----------------------------------------------------------------------------- atlas (computed)

TERM_METHODS = {
    f"term_episodes_ended_{slug}": f"count of distinct UCDP conflict episodes located in the country (gwno_loc) whose "
    f"termination (c_epterm = 1) in the year (c_ep_endyear) is coded c_outcome = {k} ({slug.replace('_', ' ')})"
    for k, slug in OUTCOME_SLUG.items()
}
PA_METHODS = {
    "pa_signed": "count of distinct UCDP peace agreements whose incompatibility is located in the country "
    "(gwno), by year of signature"
}


def term_cy_rows(
    rows: Sequence[Mapping[str, str]], *, raw_sha256: str, crosswalk: StateCodeCrosswalk
) -> list[dict[str, Any]]:
    members: dict[tuple[str, str, int, int], set[str]] = defaultdict(set)
    lines: dict[tuple[str, str, int, int], list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if r["c_epterm"] != "1" or not r["c_outcome"].isdigit() or int(r["c_outcome"]) not in OUTCOME_SLUG:
            continue
        y = int(r["c_ep_endyear"] or r["year"])
        ind = f"term_episodes_ended_{OUTCOME_SLUG[int(r['c_outcome'])]}"
        for gw in set(_codes(r["gwno_loc"])):
            members[(ind, "ucdp", gw, y)].add(r["c_epid"])
            lines[(ind, "ucdp", gw, y)].append(i + 2)
    return cd.count_atlas_rows(
        members,
        lines,
        source=ATLAS_SOURCE,
        dataset="term_cy",
        crosswalk=crosswalk,
        file=TERM_FILE,
        raw_sha256=raw_sha256,
        methods=TERM_METHODS,
        computed_from="UCDP Conflict Termination Dataset v4-2024 (conflict level)",
        geo_prefix="gw",
        id_label="c_epids",
    )


def pa_cy_rows(
    rows: Sequence[Mapping[str, str]], *, raw_sha256: str, crosswalk: StateCodeCrosswalk
) -> list[dict[str, Any]]:
    members: dict[tuple[str, str, int, int], set[str]] = defaultdict(set)
    lines: dict[tuple[str, str, int, int], list[int]] = defaultdict(list)
    for r in rows:
        if not r["year"].isdigit():
            continue
        for gw in set(_codes(r["gwno"])):
            members[("pa_signed", "ucdp", gw, int(r["year"]))].add(r["paid"])
            lines[("pa_signed", "ucdp", gw, int(r["year"]))].append(int(r["_row"]))
    return cd.count_atlas_rows(
        members,
        lines,
        source=ATLAS_SOURCE,
        dataset="pa_cy",
        crosswalk=crosswalk,
        file=PA_FILE,
        raw_sha256=raw_sha256,
        methods=PA_METHODS,
        computed_from=f"UCDP Peace Agreement Dataset {PA_VERSION}",
        geo_prefix="gw",
        id_label="paids",
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
        provider="Uppsala Conflict Data Program (Uppsala University)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST, "cran.r-project.org"),
        datasets=("term_cy", "pa_cy"),
        dataset_ids=(
            "ucdp-term-conflict-v4-2024",
            f"ucdp-peace-agreements-{PA_VERSION}",
            f"countrycode-{COUNTRYCODE_VERSION}",
        ),
        cadence_seconds=90 * 86400,
        planned_requests=7,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "geo": "Gleditsch-Ward codes, UCDP variant (gwno_loc / gwno)",
            "time": "termination year (c_ep_endyear) / signature year",
            "kg": "ucdp:episode:<c_epid>[:termination], ucdp:pa:<paid> (conflicts: KGGEOPOL's ucdp:conflict:<id>)",
            "claims": "registry/causal/relations_datasets.yaml datasets term_conflict, peace_agreements",
        },
        notes="Shares the ucdp.downloads source/budget with atlas.ucdp (KGGEOPOL). Also fetches countrycode (CRAN).",
    )


def build(raws: Mapping[str, RawAcquisition], *, crosswalk: StateCodeCrosswalk, strict: bool = True) -> dict[str, Any]:
    shas = {k: hashlib.sha256(r.body).hexdigest() for k, r in raws.items()}
    term = parse_term(raws["term"].body, strict=strict)
    pa = parse_pa(raws["pa"].body, strict=strict)
    kg = kg_and_claims(term, pa, shas=shas)
    cd.check_unique_keys(kg["statements"])
    return {
        "shas": shas,
        "term_cy": term_cy_rows(term, raw_sha256=shas["term"], crosswalk=crosswalk),
        "pa_cy": pa_cy_rows(pa, raw_sha256=shas["pa"], crosswalk=crosswalk),
        **kg,
        "row_counts": {"term": len(term), "pa": len(pa)},
    }


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the termination CSV, the peace agreement workbook, both codebooks and countrycode; archive; register
    ``ucdp.term_cy`` / ``ucdp.pa_cy``; load the KG records; deliver the claims."""
    crosswalk, _cc_sha = await fetch_countrycode(run)
    raws = {
        k: await run.get(
            u, key=f"{k}:{TERM_VERSION if k == 'term' else PA_VERSION}", media_types=MEDIA[k], max_bytes=MAX_BYTES
        )
        for k, u in URLS.items()
    }
    books = {}
    for ds, doc_key in CODEBOOKS.items():
        doc = cd.document(doc_key)
        raw = await run.get(doc["download_url"], key=doc_key, media_types=PDF_MEDIA, max_bytes=MAX_BYTES)
        books[ds] = {"raw": raw, **cd.check_document(doc_key, raw.body)}
    out = build(raws, crosswalk=crosswalk, strict=run.origin == "live")
    for raw in [*raws.values(), *(b["raw"] for b in books.values())]:
        run.archive(raw)
    shas = out["shas"]
    res: dict[str, Any] = {
        "row_counts": out["row_counts"],
        "atlas": {},
        "anomalies": out["anomalies"],
        "codebooks": {k: {x: v[x] for x in ("sha256", "matches")} for k, v in books.items()},
    }
    for ds, sha, note in (
        (
            "term_cy",
            shas["term"],
            "COMPUTED from the UCDP Conflict Termination Dataset v4-2024 (1946-2024): conflict "
            "episodes ended per location (gwno_loc) and year, by coded outcome; only country-years with >= 1 ended episode",
        ),
        (
            "pa_cy",
            shas["pa"],
            f"COMPUTED from the UCDP Peace Agreement Dataset {PA_VERSION} (1975-2021): agreements "
            "signed per location of the incompatibility (gwno) and year; only country-years with >= 1 agreement",
        ),
    ):
        if run.wants(ds):
            r = run.register(
                dataset=ds, frame=atlas_frame(out[ds]), raw_sha256s=[sha], rights=RIGHTS, coverage_note=note
            )
            res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    retrieved = max(r.retrieved_at for r in raws.values())
    if run.dataset is None and cd.kg_available(run.engine):
        res["kg"] = cd.load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision=f"ucdp-term-v4-2024+pa-{PA_VERSION}+reva-{CONNECTOR_VERSION}",
            raw_sha256s=sorted(set(shas.values())),
            retrieved_at=retrieved,
            origin=run.origin,
        )
        # an episode with several terminal rows has per-row keys ``ep<c_epid>:t<year>:*`` from 1.2.0: its earlier
        # single-row statements (``ep<c_epid>:end``, ``ep<c_epid>:t:*``) are retracted (append-only)
        multi = sorted(out["anomalies"].get("episodes_with_several_terminal_rows", {}))
        if multi:
            res["kg"]["superseded_single_terminal"] = cd.retract_statements(
                run.engine,
                run.store,
                source_key=SOURCE_KEY,
                origin=run.origin,
                # stored statement keys are "<source key>:<connector key>" (atlas_geopol_common.to_kg_model)
                like_patterns=[f"{SOURCE_KEY}:{x}" for e in multi for x in (f"ep{e}:end", f"ep{e}:t:%")],
                keep={f"{SOURCE_KEY}:{st.key}" for st in out["statements"]},
                raw_sha256s=[shas["term"]],
                retrieved_at=retrieved,
                parameters={"connector": CONNECTOR_NAME, "connector_version": CONNECTOR_VERSION},
            )
    deliveries = []
    for ds, raw_key in (("term_conflict", "term"), ("peace_agreements", "pa")):
        if not books[ds]["matches"]:
            res.setdefault("claims_withheld", {})[ds] = "codebook PDF differs from the pinned sha256"
            continue
        e = cd.dataset_entry(ds)
        deliveries.append(
            cd.ClaimDelivery(
                source_key=SOURCE_KEY,
                dataset=ds,
                dataset_revision=e["dataset_revision"],
                codebook=cd.codebook(
                    ds, {CODEBOOKS[ds]: books[ds]["sha256"]}, archived={CODEBOOKS[ds]: books[ds]["sha256"]}
                ),
                raw_sha256s=[shas[raw_key], books[ds]["sha256"]],
                retrieved_at=retrieved,
                origin=run.origin,
                claims=out["claims"][ds],
                complete=True,
            )
        )
    if run.dataset is None:
        res["claims"] = cd.deliver_claims(run, deliveries)
    return res
