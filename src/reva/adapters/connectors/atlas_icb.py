"""International Crisis Behavior (ICB) data, version 16 (agent CAUSDATA): crises as events + T1 coded claims.

Source and access
-----------------
ICB project (Brecher, Wilkenfeld, Beardsley, James, Quinn), Duke University, ``sites.duke.edu/icbdata``: version 16
(codebook dated 2025-06-20) covers 512 international crises 1918-2021 and 1,131 crisis actors. The data and
codebooks are public Box shared files (no account, no registration): ``duke.box.com/shared/static/<id>.<ext>``
redirects (3 hops: duke.box.com -> duke.app.box.com -> public.boxcloud.com) to the file. Terms: no licence is
stated; the project asks users to cite Brecher & Wilkenfeld (1997/2000) and the v16 codebook -> treated as
**cite-only, not an open licence** (``registry/sources/icb.yaml``, maintainer review flagged).

Files: ``icb1v16.csv`` (system level, one row per international crisis), ``icb2v16.csv`` (actor level, one row per
crisis actor), and the two codebook PDFs, whose sha256 must equal the pinned ones in
``registry/causal/relations_datasets.yaml`` (else the claims are not delivered: the quoted definitions might no
longer be the provider's).

Outputs (all relations explained in ``registry/causal/relations_datasets.yaml``)
-------
* KG events (``kind``): ``icb:crisis:<crisno>`` (international_crisis), its breakpoint ``…:trigger``
  (crisis_trigger) and outcome ``…:outcome`` (crisis_outcome); per actor ``icb:crisis:<crisno>:actor:<cracno>``
  (foreign_policy_crisis, part of the crisis) with ``…:trigger``, ``…:response`` (crisis_response) and
  ``…:outcome``; protracted conflicts ``icb:pc:<pcid>``. Dates as ``P580``/``P582``/``P585`` with precision
  (day 88 or blank = unknown day -> month precision).
* T1 claims (CAUSCORE ``load_dataset_claims``): ``coded_trigger`` (trigger -> crisis, BREAK / TRIGGR),
  ``reaction_to`` (actor trigger -> major response, MAJRES), ``coded_outcome`` (crisis -> outcome, FOROUT / OUTCOM).
* Atlas table ``icb.actor_cy`` (COMPUTED counts per state code and trigger year).

State codes: the codebook says ICB actor codes "correspond to those presented by Gleditsch and Ward"; they resolve
through KGGEOPOL's :class:`StateCodeCrosswalk` with the ``gw`` system (provider_geo ``icb:<code>``).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
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

SOURCE_KEY = "icb.data"
CONNECTOR_NAME = "atlas.icb"
CONNECTOR_VERSION = "1.1.0"  # 1.1.0 (AUDFIX L3): MAJRES=1 (no response-inaction) is not a reaction_to claim
PARSER_VERSION = "atlas-icb-parse-1"
ICB_VERSION = "v16"
ATLAS_SOURCE = "icb"
BOX = "https://duke.box.com/shared/static/"
HOSTS = ("duke.box.com", "duke.app.box.com", "public.boxcloud.com", "cran.r-project.org")
CSV_MEDIA = ("text/csv", "application/csv", "application/octet-stream", "text/plain")
PDF_MEDIA = ("application/pdf", "application/octet-stream")

#: key -> (Box shared id + extension, logical file name (Content-Disposition), minimum rows, required columns)
DATA_FILES: dict[str, tuple[str, str, int, tuple[str, ...]]] = {
    "icb1": (
        "h6fodyhoq2deul0v5ekuqgb4zblx557c.csv",
        "icb1v16.csv",
        500,
        (
            "icb1",
            "crisno",
            "crisname",
            "break",
            "trigent",
            "yrtrig",
            "motrig",
            "datrig",
            "yrterm",
            "moterm",
            "daterm",
            "gravcr",
            "crismg",
            "viol",
            "subout",
            "forout",
            "exsat",
            "outesr",
            "pcid",
        ),
    ),
    "icb2": (
        "oc99j12bgywnkzrpbhypkicwjcdglh8l.csv",
        "icb2v16.csv",
        1100,
        (
            "icb2",
            "crisno",
            "cracno",
            "cracid",
            "actor",
            "crisname",
            "triggr",
            "yrtrig",
            "motrig",
            "datrig",
            "trigent",
            "trigloc",
            "majres",
            "yerres",
            "monres",
            "dayres",
            "crismg",
            "outcom",
            "outfor",
            "outevl",
            "outesr",
            "yrterm",
            "moterm",
            "daterm",
            "viol",
            "gravty",
            "pcid",
        ),
    ),
}
CODEBOOKS = {"icb1": "icb1_codebook", "icb2": "icb2_codebook"}
#: MAJRES 1 "No response-inaction": coded, kept as the response-type attribute, never a reaction_to claim (L3)
NO_RESPONSE_CODE = "1"
MAX_BYTES = 5_000_000
NON_STATE = {995: "internally generated", 996: "non-state actor", 997: "more than one state"}
#: PCID 40-44: "crises that are not part of protracted conflicts" (regional buckets): no part_of statement.
PC_BUCKETS = frozenset({40, 41, 42, 43, 44})

RIGHTS: dict[str, str] = {
    "licence": "ICB terms: free download, citation requested (no open licence stated)",
    "licence_url": "https://sites.duke.edu/icbdata/data-collections/",
    "attribution": "International Crisis Behavior Project, data version 16 (sites.duke.edu/icbdata). Brecher & "
    "Wilkenfeld (1997), A Study of Crisis; Brecher, Wilkenfeld, Beardsley, James & Quinn (2025), ICB Data Codebook v16",
}


def data_url(key: str) -> str:
    return BOX + DATA_FILES[key][0]


def file_name(key: str) -> str:
    return DATA_FILES[key][1]


def _int(v: Any) -> int | None:
    s = str(v or "").strip()
    if not s or not s.lstrip("-").isdigit():
        return None
    return int(s)


def parse(key: str, body: bytes, *, strict: bool = True) -> list[dict[str, str]]:
    _id, name, min_rows, required = DATA_FILES[key]
    rows = cd.read_csv_bytes(body, name=name, required=required)
    if strict and len(rows) < min_rows:
        raise cd.drift(f"ICB {name}: {len(rows)} rows < expected {min_rows} (truncated?)")
    for i, r in enumerate(rows):
        if _int(r["crisno"]) is None or (key == "icb2" and _int(r["cracno"]) is None):
            raise cd.drift(f"ICB {name} row {i + 2}: missing crisno/cracno")
    return rows


def _d(r: Mapping[str, str], y: str, m: str, d: str) -> tuple[str, int] | None:
    return cd.coded_date(r.get(y), r.get(m), r.get(d))


class _Builder:
    def __init__(self, shas: Mapping[str, str], resolver: StateEntityResolver) -> None:
        self.shas = shas
        self.resolver = resolver
        self.ents: dict[str, KgEntity] = {}
        self.sts: list[KgStatement] = []
        self.claims: dict[str, list[cd.ClaimRecord]] = {"icb1": [], "icb2": []}
        self.inaction_no_claim = 0  # MAJRES = 1 rows: response coded, no reaction_to claim (audit L3)

    def ent(self, eid: str, kind: str, label: str, desc: str) -> None:
        self.ents[eid] = KgEntity(qid=eid, kind=kind, labels={"en": label}, descriptions={"en": desc})

    def add(self, st: KgStatement | None) -> None:
        if st is not None:
            self.sts.append(st)

    def kind(self, ds: str, key: str, eid: str, kind: str, sel: Mapping[str, Any]) -> None:
        self.add(
            cd.string_stmt(
                f"{key}:event_type", eid, "causdata:event_type", kind, raw_sha256=self.shas[ds], selector=sel
            )
        )

    def trigent(
        self, ds: str, key: str, eid: str, code: int | None, when: tuple[str, int] | None, sel: Mapping[str, Any]
    ) -> None:
        if code is None:
            return
        if code in NON_STATE:
            self.add(
                cd.string_stmt(
                    f"{key}:trigent",
                    eid,
                    "icb:triggering_entity_kind",
                    NON_STATE[code],
                    raw_sha256=self.shas[ds],
                    selector=sel,
                    code=str(code),
                    variable="TRIGENT",
                )
            )
            return
        at = date.fromisoformat(when[0]) if when else date(1950, 7, 1)
        obj = self.resolver.entity("gw", code, at)
        self.add(
            KgStatement(
                key=f"{key}:trigent",
                subject=eid,
                property="icb:triggering_entity",
                object_qid=obj,
                qualifiers={"code": str(code), "variable": "TRIGENT", "code_system": "ICB (Gleditsch-Ward)"},
                raw_sha256=self.shas[ds],
                selector=dict(sel),
            )
        )


def kg_and_claims(
    icb1: Sequence[Mapping[str, str]],
    icb2: Sequence[Mapping[str, str]],
    *,
    shas: Mapping[str, str],
    resolver: StateEntityResolver,
) -> _Builder:
    """Entities, context statements and T1 claim records for both levels (see module doc)."""
    b = _Builder(shas, resolver)
    names = {r["crisno"]: r["crisname"] for r in icb1}
    pcs: set[int] = set()
    f1, f2 = file_name("icb1"), file_name("icb2")
    for i, r in enumerate(icb1):
        n = int(r["crisno"])
        line = i + 2
        cid = f"icb:crisis:{n}"
        sel = {"file": f1, "row": line, "crisno": n}
        name = r["crisname"] or f"ICB crisis {n}"
        trig = _d(r, "yrtrig", "motrig", "datrig")
        term = _d(r, "yrterm", "moterm", "daterm")
        coded = bool(r["break"] or r["yrtrig"])
        b.ent(
            cid,
            "international_crisis",
            name,
            f"International crisis {n} of the International Crisis Behavior data {ICB_VERSION}"
            + ("" if coded else " (placeholder: no coded variables in this version)"),
        )
        b.kind("icb1", f"c{n}", cid, "international_crisis", sel)
        for st in cd.event_times(
            cid,
            f"c{n}",
            start=trig,
            end=term,
            raw_sha256=shas["icb1"],
            selector={**sel, "columns": ["yrtrig", "motrig", "datrig", "yrterm", "moterm", "daterm"]},
            qualifiers={
                "definition": "start = date of perception of the crisis breakpoint by the "
                "earliest actor (TRIGDATE); end = termination for the last actor (TERMDATE)"
            },
        ):
            b.add(st)
        for pid, var in (("icb:gravity", "GRAVCR"), ("icb:violence", "VIOL"), ("icb:crisis_management", "CRISMG")):
            b.add(
                cd.coded_stmt(
                    f"c{n}:{var.lower()}",
                    cid,
                    pid,
                    "icb1",
                    var,
                    r[var.lower()],
                    raw_sha256=shas["icb1"],
                    selector={**sel, "column": var.lower()},
                )
            )
        pc = _int(r["pcid"])
        if pc is not None and pc not in PC_BUCKETS and cd.value_label("icb1", "PCID", pc):
            pcs.add(pc)
            b.add(
                KgStatement(
                    key=f"c{n}:pcid",
                    subject=cid,
                    property="P361",
                    object_qid=f"icb:pc:{pc}",
                    qualifiers={"variable": "PCID", "code": str(pc)},
                    raw_sha256=shas["icb1"],
                    selector={**sel, "column": "pcid"},
                )
            )
        if not coded:
            continue
        # breakpoint (system-level trigger)
        tid = f"{cid}:trigger"
        brk = cd.value_label("icb1", "BREAK", r["break"])
        b.ent(
            tid,
            "crisis_trigger",
            f"Breakpoint of {name}" + (f": {brk}" if brk else ""),
            f"Trigger (breakpoint) of ICB international crisis {n}, as coded by ICB {ICB_VERSION} (BREAK, TRIGENT)",
        )
        b.kind("icb1", f"c{n}:t", tid, "crisis_trigger", sel)
        for st in cd.event_times(
            tid,
            f"c{n}:t",
            point=trig,
            raw_sha256=shas["icb1"],
            selector={**sel, "columns": ["yrtrig", "motrig", "datrig"]},
            qualifiers={"definition": "date of perception of the breakpoint by the earliest actor"},
        ):
            b.add(st)
        b.add(
            cd.coded_stmt(
                f"c{n}:t:type",
                tid,
                "icb:trigger_type",
                "icb1",
                "BREAK",
                r["break"],
                raw_sha256=shas["icb1"],
                selector={**sel, "column": "break"},
            )
        )
        b.trigent("icb1", f"c{n}:t", tid, _int(r["trigent"]), trig, {**sel, "column": "trigent"})
        if brk:
            b.claims["icb1"].append(
                cd.ClaimRecord(
                    cause_id=tid,
                    effect_id=cid,
                    relation="coded_trigger",
                    selector={
                        "file": f1,
                        "row": line,
                        "crisno": n,
                        "columns": ["break", "trigent", "yrtrig", "motrig", "datrig"],
                    },
                    raw_sha256=shas["icb1"],
                    codebook_term="BREAK",
                    source_claim_id=f"{n}:break",
                )
            )
        # outcome (system level)
        form = cd.value_label("icb1", "FOROUT", r["forout"])
        if form:
            oid = f"{cid}:outcome"
            b.ent(
                oid,
                "crisis_outcome",
                f"Outcome of {name}: {form}",
                f"Termination and outcome of ICB international crisis {n} (FOROUT, SUBOUT, EXSAT, OUTESR; ICB {ICB_VERSION})",
            )
            b.kind("icb1", f"c{n}:o", oid, "crisis_outcome", sel)
            for st in cd.event_times(
                oid,
                f"c{n}:o",
                point=term,
                raw_sha256=shas["icb1"],
                selector={**sel, "columns": ["yrterm", "moterm", "daterm"]},
                qualifiers={"definition": "date of crisis termination (TERMDATE)"},
            ):
                b.add(st)
            for pid, var in (
                ("icb:outcome_form", "FOROUT"),
                ("icb:outcome_content", "SUBOUT"),
                ("icb:outcome_satisfaction", "EXSAT"),
                ("icb:tension_after_outcome", "OUTESR"),
            ):
                b.add(
                    cd.coded_stmt(
                        f"c{n}:o:{var.lower()}",
                        oid,
                        pid,
                        "icb1",
                        var,
                        r[var.lower()],
                        raw_sha256=shas["icb1"],
                        selector={**sel, "column": var.lower()},
                    )
                )
            b.claims["icb1"].append(
                cd.ClaimRecord(
                    cause_id=cid,
                    effect_id=oid,
                    relation="coded_outcome",
                    selector={
                        "file": f1,
                        "row": line,
                        "crisno": n,
                        "columns": ["forout", "subout", "exsat", "outesr", "yrterm", "moterm", "daterm"],
                    },
                    raw_sha256=shas["icb1"],
                    codebook_term="FOROUT",
                    source_claim_id=f"{n}:forout",
                )
            )
    for pc in sorted(pcs):
        b.ent(
            f"icb:pc:{pc}",
            "protracted_conflict",
            f"{cd.value_label('icb1', 'PCID', pc)} (ICB protracted conflict {pc})",
            f"Protracted conflict {pc} as coded by ICB {ICB_VERSION} (PCID)",
        )
        b.kind("icb1", f"pc{pc}", f"icb:pc:{pc}", "protracted_conflict", {"file": f1, "column": "pcid", "value": pc})

    for i, r in enumerate(icb2):
        n, a = int(r["crisno"]), int(r["cracno"])
        line = i + 2
        cid = f"icb:crisis:{n}"
        aid = f"{cid}:actor:{a}"
        sel = {"file": f2, "row": line, "crisno": n, "cracno": a}
        cname = names.get(r["crisno"]) or r["crisname"] or f"ICB crisis {n}"
        actor = r["actor"]
        trig = _d(r, "yrtrig", "motrig", "datrig")
        term = _d(r, "yrterm", "moterm", "daterm")
        resp = _d(r, "yerres", "monres", "dayres")
        b.ent(
            aid,
            "foreign_policy_crisis",
            f"{cname}: crisis for {actor}",
            f"Foreign policy crisis of crisis actor {actor} (ICB code {r['cracid']}) in ICB international crisis {n} "
            f"(ICB {ICB_VERSION}, actor case {a})",
        )
        b.kind("icb2", f"a{a}", aid, "foreign_policy_crisis", sel)
        for st in cd.event_times(
            aid,
            f"a{a}",
            start=trig,
            end=term,
            raw_sha256=shas["icb2"],
            selector={**sel, "columns": ["yrtrig", "motrig", "datrig", "yrterm", "moterm", "daterm"]},
            qualifiers={
                "definition": "start = date the actor perceived the trigger (TRIGDATE); end = "
                "date of termination of the crisis for the actor (TERMDATE)"
            },
        ):
            b.add(st)
        b.add(
            KgStatement(
                key=f"a{a}:part_of",
                subject=aid,
                property="P361",
                object_qid=cid,
                qualifiers={"variable": "CRISNO"},
                raw_sha256=shas["icb2"],
                selector=dict(sel),
            )
        )
        code = _int(r["cracid"])
        if code is not None:
            at = date.fromisoformat(trig[0]) if trig else date(1950, 7, 1)
            b.add(
                KgStatement(
                    key=f"a{a}:actor",
                    subject=aid,
                    property="P710",
                    object_qid=resolver.entity("gw", code, at, provider_label=actor),
                    qualifiers={"role": "crisis actor", "icb_actor": actor, "code": str(code), "variable": "CRACID"},
                    raw_sha256=shas["icb2"],
                    selector={**sel, "column": "cracid"},
                )
            )
        b.add(
            cd.coded_stmt(
                f"a{a}:gravty",
                aid,
                "icb:gravity",
                "icb2",
                "GRAVTY",
                r["gravty"],
                raw_sha256=shas["icb2"],
                selector={**sel, "column": "gravty"},
            )
        )
        # actor trigger
        tid = f"{aid}:trigger"
        tt = cd.value_label("icb2", "TRIGGR", r["triggr"])
        if tt:
            b.ent(
                tid,
                "crisis_trigger",
                f"Trigger of {actor}'s crisis in {cname}: {tt}",
                f"Trigger to the foreign policy crisis of {actor} in ICB crisis {n} (TRIGGR, TRIGENT; ICB {ICB_VERSION})",
            )
            b.kind("icb2", f"a{a}:t", tid, "crisis_trigger", sel)
            for st in cd.event_times(
                tid,
                f"a{a}:t",
                point=trig,
                raw_sha256=shas["icb2"],
                selector={**sel, "columns": ["yrtrig", "motrig", "datrig"]},
                qualifiers={"definition": "date the actor perceived the trigger (TRIGDATE)"},
            ):
                b.add(st)
            b.add(
                cd.coded_stmt(
                    f"a{a}:t:type",
                    tid,
                    "icb:trigger_type",
                    "icb2",
                    "TRIGGR",
                    r["triggr"],
                    raw_sha256=shas["icb2"],
                    selector={**sel, "column": "triggr"},
                )
            )
            b.add(
                cd.coded_stmt(
                    f"a{a}:t:loc",
                    tid,
                    "icb:trigger_location",
                    "icb2",
                    "TRIGLOC",
                    r["trigloc"],
                    raw_sha256=shas["icb2"],
                    selector={**sel, "column": "trigloc"},
                )
            )
            b.trigent("icb2", f"a{a}:t", tid, _int(r["trigent"]), trig, {**sel, "column": "trigent"})
            b.claims["icb2"].append(
                cd.ClaimRecord(
                    cause_id=tid,
                    effect_id=aid,
                    relation="coded_trigger",
                    selector={
                        "file": f2,
                        "row": line,
                        "crisno": n,
                        "cracno": a,
                        "columns": ["triggr", "trigent", "trigloc", "yrtrig", "motrig", "datrig"],
                    },
                    raw_sha256=shas["icb2"],
                    codebook_term="TRIGGR",
                    source_claim_id=f"{n}:{a}:triggr",
                )
            )
            # major response to the trigger
            rt = cd.value_label("icb2", "MAJRES", r["majres"])
            if rt:
                rid = f"{aid}:response"
                b.ent(
                    rid,
                    "crisis_response",
                    f"Major response of {actor} in {cname}: {rt}",
                    f"Major response of {actor} to its crisis trigger in ICB crisis {n} (MAJRES; ICB {ICB_VERSION})",
                )
                b.kind("icb2", f"a{a}:r", rid, "crisis_response", sel)
                for st in cd.event_times(
                    rid,
                    f"a{a}:r",
                    point=resp,
                    raw_sha256=shas["icb2"],
                    selector={**sel, "columns": ["yerres", "monres", "dayres"]},
                    qualifiers={"definition": "date the major response was formulated (RESPDATE)"},
                ):
                    b.add(st)
                b.add(
                    cd.coded_stmt(
                        f"a{a}:r:type",
                        rid,
                        "icb:response_type",
                        "icb2",
                        "MAJRES",
                        r["majres"],
                        raw_sha256=shas["icb2"],
                        selector={**sel, "column": "majres"},
                    )
                )
                if str(r["majres"]).strip() == NO_RESPONSE_CODE:
                    # inaction is coded (attribute icb:response_type above) but is not a reaction: one policy with
                    # MMP "ignore" (audit L3, docs/dev/v3_audfix.md)
                    b.inaction_no_claim += 1
                else:
                    b.claims["icb2"].append(
                        cd.ClaimRecord(
                            cause_id=tid,
                            effect_id=rid,
                            relation="reaction_to",
                            selector={
                                "file": f2,
                                "row": line,
                                "crisno": n,
                                "cracno": a,
                                "columns": ["majres", "yerres", "monres", "dayres"],
                            },
                            raw_sha256=shas["icb2"],
                            codebook_term="MAJRES",
                            source_claim_id=f"{n}:{a}:majres",
                        )
                    )
        # actor outcome
        oc = cd.value_label("icb2", "OUTCOM", r["outcom"])
        if oc:
            oid = f"{aid}:outcome"
            b.ent(
                oid,
                "crisis_outcome",
                f"Outcome for {actor} of {cname}: {oc}",
                f"Termination and outcome of the crisis for {actor} in ICB crisis {n} (OUTCOM, OUTFOR, OUTEVL; ICB {ICB_VERSION})",
            )
            b.kind("icb2", f"a{a}:o", oid, "crisis_outcome", sel)
            for st in cd.event_times(
                oid,
                f"a{a}:o",
                point=term,
                raw_sha256=shas["icb2"],
                selector={**sel, "columns": ["yrterm", "moterm", "daterm"]},
                qualifiers={"definition": "date of termination of the crisis for the actor (TERMDATE)"},
            ):
                b.add(st)
            for pid, var in (
                ("icb:outcome_content", "OUTCOM"),
                ("icb:outcome_form", "OUTFOR"),
                ("icb:outcome_satisfaction", "OUTEVL"),
                ("icb:tension_after_outcome", "OUTESR"),
            ):
                b.add(
                    cd.coded_stmt(
                        f"a{a}:o:{var.lower()}",
                        oid,
                        pid,
                        "icb2",
                        var,
                        r[var.lower()],
                        raw_sha256=shas["icb2"],
                        selector={**sel, "column": var.lower()},
                    )
                )
            b.claims["icb2"].append(
                cd.ClaimRecord(
                    cause_id=aid,
                    effect_id=oid,
                    relation="coded_outcome",
                    selector={
                        "file": f2,
                        "row": line,
                        "crisno": n,
                        "cracno": a,
                        "columns": ["outcom", "outfor", "outevl", "outesr", "yrterm", "moterm", "daterm"],
                    },
                    raw_sha256=shas["icb2"],
                    codebook_term="OUTCOM",
                    source_claim_id=f"{n}:{a}:outcom",
                )
            )
    return b


# ----------------------------------------------------------------------------- atlas: actor_cy (computed)

ACTOR_CY_METHODS = {
    "icb_crises_as_actor": "count of distinct ICB international crises in which the state is a crisis actor (ICB2 "
    "rows), by the year the actor perceived its trigger (YRTRIG)",
    "icb_crises_triggered": "count of distinct ICB international crises in which the state is coded as the triggering "
    "entity (TRIGENT) of at least one actor's crisis, by that actor's trigger year",
    "icb_crises_as_actor_war": "count of distinct ICB international crises in which the state is a crisis actor and "
    "the actor-level VIOL is 4 (full-scale war), by trigger year",
}


def actor_cy_rows(
    rows: Sequence[Mapping[str, str]], *, raw_sha256: str, crosswalk: StateCodeCrosswalk
) -> list[dict[str, Any]]:
    members: dict[tuple[str, str, int, int], set[str]] = defaultdict(set)
    lines: dict[tuple[str, str, int, int], list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        y = _int(r["yrtrig"])
        if y is None:
            continue
        n = r["crisno"]
        roles: list[tuple[str, int | None]] = [("icb_crises_as_actor", _int(r["cracid"]))]
        t = _int(r["trigent"])
        if t is not None and t not in NON_STATE:
            roles.append(("icb_crises_triggered", t))
        if r["viol"] == "4":
            roles.append(("icb_crises_as_actor_war", _int(r["cracid"])))
        for ind, code in roles:
            if code is None:
                continue
            members[(ind, "gw", code, y)].add(n)
            lines[(ind, "gw", code, y)].append(i + 2)
    return cd.count_atlas_rows(
        members,
        lines,
        source=ATLAS_SOURCE,
        dataset="actor_cy",
        crosswalk=crosswalk,
        file=file_name("icb2"),
        raw_sha256=raw_sha256,
        methods=ACTOR_CY_METHODS,
        computed_from=f"ICB actor-level data {ICB_VERSION} (icb2v16.csv)",
        geo_prefix="icb",
        id_label="crisno",
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
        provider="International Crisis Behavior Project (Duke University / University of Maryland)",
        licence=RIGHTS["licence"],
        allowed_hosts=HOSTS,
        datasets=("actor_cy",),
        dataset_ids=(f"icb1-{ICB_VERSION}", f"icb2-{ICB_VERSION}", f"countrycode-{COUNTRYCODE_VERSION}"),
        cadence_seconds=90 * 86400,
        planned_requests=8,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "geo": "ICB actor codes (Gleditsch-Ward per the codebook), provider_geo icb:<code>",
            "time": "trigger year (YRTRIG) for the atlas counts; KG dates at day/month precision",
            "kg": "icb:crisis:<crisno>[:actor:<cracno>][:trigger|:response|:outcome], icb:pc:<pcid>",
            "claims": "registry/causal/relations_datasets.yaml datasets icb1, icb2",
        },
        notes="Box shared files (public, no account). Codebook PDFs are fetched and checked against pinned sha256 "
        "before claims are delivered. Also fetches the countrycode R package (CRAN) for state codes.",
    )


def build(
    raws: Mapping[str, RawAcquisition],
    *,
    crosswalk: StateCodeCrosswalk,
    qid_for_iso3: Mapping[str, str],
    countrycode_sha256: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Parse raw bytes (keys ``icb1``, ``icb2``) into the atlas rows, KG records and claim records (no I/O)."""
    shas = {k: hashlib.sha256(r.body).hexdigest() for k, r in raws.items()}
    parsed = {k: parse(k, raws[k].body, strict=strict) for k in ("icb1", "icb2")}
    resolver = StateEntityResolver(crosswalk, qid_for_iso3)
    b = kg_and_claims(parsed["icb1"], parsed["icb2"], shas=shas, resolver=resolver)
    stmts = b.sts
    if countrycode_sha256:
        stmts = stmts + code_correspondence(resolver, raw_sha256=countrycode_sha256)
    ents = list(b.ents.values()) + resolver.code_entities()
    cd.check_unique_keys(stmts)
    return {
        "shas": shas,
        "actor_cy": actor_cy_rows(parsed["icb2"], raw_sha256=shas["icb2"], crosswalk=crosswalk),
        "entities": ents,
        "statements": stmts,
        "claims": b.claims,
        "majres_inaction_no_claim": b.inaction_no_claim,
        "row_counts": {k: len(v) for k, v in parsed.items()},
        "unresolved_state_codes": {k: sorted(v) for k, v in resolver.unresolved.items()},
    }


async def fetch_codebooks(run: Any) -> dict[str, dict[str, Any]]:
    out = {}
    for ds, doc_key in CODEBOOKS.items():
        doc = cd.document(doc_key)
        raw = await run.get(
            doc["download_url"], key=f"{doc_key}:{ICB_VERSION}", media_types=PDF_MEDIA, max_bytes=MAX_BYTES
        )
        out[ds] = {"raw": raw, **cd.check_document(doc_key, raw.body)}
    return out


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch ICB1/ICB2 v16 + codebooks + countrycode through the run, archive them, register ``icb.actor_cy``, load
    the KG records and deliver the T1 claims (or stage them, see ``causdata_common.deliver_claims``)."""
    crosswalk, cc_sha = await fetch_countrycode(run)
    raws = {}
    for k in DATA_FILES:
        raws[k] = await run.get(data_url(k), key=f"{k}:{ICB_VERSION}", media_types=CSV_MEDIA, max_bytes=MAX_BYTES)
    books = await fetch_codebooks(run)
    out = build(
        raws,
        crosswalk=crosswalk,
        qid_for_iso3=load_qid_for_iso3(),
        countrycode_sha256=cc_sha,
        strict=run.origin == "live",
    )
    for raw in [*raws.values(), *(b["raw"] for b in books.values())]:  # parsed first: nothing archived on drift
        run.archive(raw)
    shas = out["shas"]
    res: dict[str, Any] = {
        "row_counts": out["row_counts"],
        "majres_inaction_no_claim": out["majres_inaction_no_claim"],
        "atlas": {},
        "codebooks": {k: {x: v[x] for x in ("sha256", "matches")} for k, v in books.items()},
    }
    if run.wants("actor_cy"):
        r = run.register(
            dataset="actor_cy",
            frame=atlas_frame(out["actor_cy"]),
            raw_sha256s=[shas["icb2"]],
            rights=RIGHTS,
            coverage_note=f"COMPUTED from ICB actor-level data {ICB_VERSION} (1918-2021): crises per state and "
            "trigger year (as crisis actor, as triggering entity, as actor in a full-scale-war crisis); only "
            "state-years with >= 1 case are listed",
        )
        res["atlas"]["actor_cy"] = {k: r[k] for k in ("dataset_key", "row_count", "created", "unmapped_provider_geo")}
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    retrieved = max(r.retrieved_at for r in raws.values())
    if run.dataset is None and cd.kg_available(run.engine):
        res["kg"] = cd.load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision=f"icb-{ICB_VERSION}",
            raw_sha256s=sorted(set(shas.values()) | {cc_sha}),
            retrieved_at=retrieved,
            origin=run.origin,
        )
    deliveries = []
    for ds in ("icb1", "icb2"):
        if not books[ds]["matches"]:
            res.setdefault("claims_withheld", {})[ds] = "codebook PDF differs from the pinned sha256"
            continue
        e = cd.dataset_entry(ds)
        deliveries.append(
            cd.ClaimDelivery(
                source_key=SOURCE_KEY,
                dataset=ds,
                dataset_revision=e["dataset_revision"],
                codebook=cd.codebook(ds, {CODEBOOKS[ds]: books[ds]["sha256"]}),
                raw_sha256s=[shas[ds]],
                retrieved_at=retrieved,
                origin=run.origin,
                claims=out["claims"][ds],
                complete=True,
            )
        )
    if run.dataset is None:
        res["claims"] = cd.deliver_claims(run, deliveries)
    res["unresolved_state_codes"] = {
        k: v[:3] + (["..."] if len(v) > 3 else []) for k, v in out["unresolved_state_codes"].items()
    }
    return res
