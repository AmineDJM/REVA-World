"""UCDP Conflict Termination v4-2024, dyad level: warring-dyad episodes and how each ended (T1 ``coded_outcome``)
(agent CAUSMORE; the conflict-level file is CAUSDATA's :mod:`atlas_ucdp_term`).

Source and access
-----------------
``ucdp.uu.se/downloads/monadterm/UCDPConflictTerminationDataset_v4_2024_Dyad.csv`` (no account; CC BY 4.0, "All
datasets are free of charge and licensed under CC BY 4.0"), registered source ``ucdp.downloads``, plus the codebook
PDF shared with the conflict-level file (sha256 pinned as ``ucdp_term_codebook`` in
``registry/causal/relations_causmore.yaml``: a changed codebook withholds the claims). 2 requests per run.

What is built (reasons: ``relations_causmore.yaml`` dataset ``ucdp_term_dyad``)
-------------
* ``ucdp:dyadepisode:<d_epid>`` (``causmore:event_type`` = ``dyad_conflict_episode``): ``P361`` part of KGGEOPOL's
  ``ucdp:conflict:<conflict_id>``, ``P155`` follows the previous episode of the same dyad, ``P710`` the two sides
  (KGGEOPOL's ``ucdp:actor:<id>``), the incompatibility as ``causdata:coded_issue`` (an issue, not a cause), ``P580``
  episode start / ``P582`` termination date at the coded precision.
* ``ucdp:dyadepisode:<d_epid>:termination`` (``conflict_termination``): ``P585`` = ``d_ependdate``, the outcome
  (``ucdpterm:outcome``), the ceasefire / peace agreement dates the file gives "at, or after, the end of the dyad
  episode" (``ucdpterm:dyad_ceasefire_date`` / ``ucdpterm:dyad_peace_agreement_date``: dated facts, no agreement id,
  so never joined to an agreement record).
* T1 claim ``coded_outcome`` dyad episode -> termination, codebook term ``d_outcome=<k>``.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors import causmore_common as cm
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement

NAME = "atlas.ucdp_dyadterm"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_ucdp_dyadterm/1"
SOURCE_KEY = "ucdp.downloads"
ATLAS_SOURCE = "ucdp"
DATASET = "dyadterm_kg"
CLAIMS_DATASET = "ucdp_term_dyad"
CODEBOOK_KEY = "ucdp_term_codebook"
HOST = "ucdp.uu.se"
DATA_FILE = "UCDPConflictTerminationDataset_v4_2024_Dyad.csv"
DATA_URL = f"https://{HOST}/downloads/monadterm/{DATA_FILE}"
TERM_VERSION = "4.2024002"
MEDIA = ("text/csv", "application/csv", "application/octet-stream", "text/plain")
PDF_MEDIA = ("application/pdf", "application/octet-stream")
MAX_BYTES = 8_000_000
MIN_ROWS = 3000
REQUIRED = (
    "dyad_id", "conflict_id", "location", "side_a", "side_a_id", "side_b", "side_b_id", "incompatibility",
    "territory_name", "year", "start_date2", "start_prec2", "d_epid", "d_epno", "d_ep_startyear", "d_epterm",
    "d_outcome", "d_ep_endyear", "d_ependdate", "d_ependprec", "d_cfiredate", "d_peagdate", "version",
)  # fmt: skip
#: UCDP date-precision codes -> Wikibase precision (as CAUSDATA's atlas_ucdp_term / KGGEOPOL).
PREC = {"1": 11, "2": 10, "3": 10, "4": 9, "5": 9, "6": 9, "7": 9}
INCOMPAT = {"1": "Territory", "2": "Government", "3": "Government and territory"}

RIGHTS: dict[str, Any] = {
    "licence": "CC BY 4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "Uppsala Conflict Data Program (UCDP): Conflict Termination Dataset v4-2024, dyad level "
    "(Kreutz 2010, JPR 47(2))",
}


def dated(text: Any, prec_code: Any) -> tuple[str, int] | None:
    iso = cd.iso_date(text)
    if iso is None:
        return None
    p = PREC.get(str(prec_code or "").strip(), 9)
    if p == 10:
        iso = iso[:8] + "01"
    elif p == 9:
        iso = iso[:5] + "01-01"
    return iso, p


def parse(body: bytes, *, strict: bool = True) -> list[dict[str, str]]:
    rows = cd.read_csv_bytes(body, name=DATA_FILE, required=REQUIRED)
    for i, r in enumerate(rows):
        r["_line"] = str(i + 2)
    if strict and len(rows) < MIN_ROWS:
        raise cd.drift(f"{DATA_FILE}: {len(rows)} rows < {MIN_ROWS} (truncated?)")
    if {r["version"] for r in rows} - {TERM_VERSION}:
        raise cd.drift(f"{DATA_FILE}: unexpected version {sorted({r['version'] for r in rows})}")
    return rows


def build_kg(rows: Sequence[Mapping[str, str]], *, sha: str) -> dict[str, Any]:
    ents: dict[str, KgEntity] = {}
    sts: list[KgStatement] = []
    claims: list[cd.ClaimRecord] = []
    anomalies: dict[str, list[Any]] = defaultdict(list)
    eps: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for r in rows:
        eps[r["d_epid"]].append(r)
    prev: dict[str, str] = {}
    order = sorted(eps.items(), key=lambda x: (int(x[1][0]["dyad_id"]), int(x[1][0]["d_epno"] or 0)))
    for epid, erows in order:
        erows = sorted(erows, key=lambda r: int(r["year"]))
        first = erows[0]
        dyad, cid = first["dyad_id"], first["conflict_id"]
        eid = f"ucdp:dyadepisode:{epid}"
        lines = [int(r["_line"]) for r in erows]
        sel = {"file": DATA_FILE, "lines": lines[:50], "d_epid": epid}
        p = f"de{epid}"
        label = f"{first['side_a']} vs {first['side_b']} - episode {first['d_epno']} (UCDP dyad {dyad})"
        years = [int(r["year"]) for r in erows]
        ents[eid] = KgEntity(
            qid=eid,
            kind="armed_conflict_episode",
            labels={"en": label},
            descriptions={
                "en": f"Episode {first['d_epno']} of UCDP conflict dyad {dyad} (conflict {cid}, {first['location']}), "
                f"active {min(years)}-{max(years)} (UCDP Conflict Termination Dataset v4-2024, dyad level)"
            },
        )
        sts.append(
            cm.string_stmt(
                f"{p}:type", eid, "causmore:event_type", "dyad_conflict_episode", raw_sha256=sha, selector=sel
            )
        )
        sts.append(
            cm.item_stmt(
                f"{p}:part_of",
                eid,
                "P361",
                f"ucdp:conflict:{int(cid)}",
                raw_sha256=sha,
                selector={**sel, "column": "conflict_id"},
            )
        )
        for col, side in (("side_a_id", "a"), ("side_b_id", "b")):
            for aid in sorted({x.strip() for r in erows for x in r[col].split(",") if x.strip().isdigit()}):
                sts.append(
                    cm.item_stmt(
                        f"{p}:side_{side}:{aid}",
                        eid,
                        "P710",
                        f"ucdp:actor:{aid}",
                        raw_sha256=sha,
                        selector={**sel, "column": col},
                        side=side.upper(),
                        name_in_source=first["side_" + side] or None,
                    )
                )
        if first["incompatibility"] in INCOMPAT:
            sts.append(
                cm.string_stmt(
                    f"{p}:issue",
                    eid,
                    "causdata:coded_issue",
                    INCOMPAT[first["incompatibility"]],
                    raw_sha256=sha,
                    selector={**sel, "column": "incompatibility"},
                    variable="incompatibility",
                    code=first["incompatibility"],
                    territory=first["territory_name"] or None,
                    note="UCDP incompatibility: the contested issue, not a cause",
                )
            )
        if dyad in prev:
            sts.append(
                cm.item_stmt(
                    f"{p}:follows",
                    eid,
                    "P155",
                    f"ucdp:dyadepisode:{prev[dyad]}",
                    raw_sha256=sha,
                    selector=sel,
                    basis="d_epno order within the dyad (not a causal link)",
                )
            )
        prev[dyad] = epid
        terms = [r for r in erows if r["d_epterm"] == "1"]
        if len(terms) > 1:
            anomalies["episodes_with_several_terminal_rows"].append(epid)
        term = terms[-1] if terms else None
        start = dated(first["start_date2"], first["start_prec2"]) or cd.coded_date(first["d_ep_startyear"])
        end = dated(term["d_ependdate"], term["d_ependprec"]) if term else None
        sts.extend(
            cd.event_times(
                eid,
                f"{p}:t",
                start=start,
                end=end,
                raw_sha256=sha,
                selector={**sel, "columns": ["start_date2", "start_prec2", "d_ependdate", "d_ependprec"]},
                qualifiers={
                    "definition": "start = dyad start (start_date2) or first year of the episode; "
                    "end = d_ependdate (termination)"
                },
            )
        )
        if term is None:
            continue
        oc = int(term["d_outcome"]) if term["d_outcome"].isdigit() else None
        olabel = cm.value_label(CLAIMS_DATASET, "d_outcome", oc)
        if oc is None or olabel is None:
            anomalies["terminated_without_outcome"].append(epid)
            continue
        tid = f"{eid}:termination"
        tsel = {"file": DATA_FILE, "line": int(term["_line"]), "d_epid": epid}
        ents[tid] = KgEntity(
            qid=tid,
            kind="conflict_termination",
            labels={"en": f"End of {label}: {olabel}"},
            descriptions={
                "en": f"Termination of UCDP dyad episode {epid} coded d_outcome = {oc} (UCDP Conflict "
                "Termination Dataset v4-2024, dyad level)"
            },
        )
        sts.append(
            cm.string_stmt(
                f"{p}:t:type", tid, "causmore:event_type", "conflict_termination", raw_sha256=sha, selector=tsel
            )
        )
        sts.extend(
            cd.event_times(
                tid,
                f"{p}:t:t",
                point=end,
                raw_sha256=sha,
                selector={**tsel, "columns": ["d_ependdate", "d_ependprec"]},
                qualifiers={
                    "definition": "The date, as precise as possible, when the conflict dyad is terminated.",
                    "ucdp_ependprec": term["d_ependprec"],
                },
            )
        )
        sts.append(
            cm.string_stmt(
                f"{p}:t:outcome",
                tid,
                "ucdpterm:outcome",
                olabel,
                raw_sha256=sha,
                selector={**tsel, "column": "d_outcome"},
                code=str(oc),
                variable="d_outcome",
            )
        )
        for col, pid in (
            ("d_cfiredate", "ucdpterm:dyad_ceasefire_date"),
            ("d_peagdate", "ucdpterm:dyad_peace_agreement_date"),
        ):
            iso = cd.iso_date(term[col])
            if iso:
                sts.append(
                    KgStatement(
                        key=f"{p}:t:{col}",
                        subject=tid,
                        property=pid,
                        value=cd.time_value(iso, 11),
                        qualifiers={
                            "variable": col,
                            "note": "at, or after, the end of the dyad episode (codebook); "
                            "no agreement id: not linked to an agreement record",
                        },
                        raw_sha256=sha,
                        selector={**tsel, "column": col},
                    )
                )
        claims.append(
            cd.ClaimRecord(
                cause_id=eid,
                effect_id=tid,
                relation="coded_outcome",
                selector={**tsel, "columns": ["d_outcome", "d_ependdate", "d_ependprec", "d_epterm"]},
                raw_sha256=sha,
                codebook_term=f"d_outcome={oc}",
                source_claim_id=f"{epid}:d_outcome",
            )
        )
    return {
        "entities": list(ents.values()),
        "statements": sts,
        "claims": claims,
        "anomalies": dict(anomalies),
        "episodes": len(eps),
    }


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Uppsala Conflict Data Program (Uppsala University)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=("ucdp-term-dyad-v4-2024",),
        cadence_seconds=90 * 86400,
        planned_requests=3,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "kg": "ucdp:dyadepisode:<d_epid>[:termination] (conflicts / actors: KGGEOPOL's ucdp:conflict / ucdp:actor)",
            "claims": "registry/causal/relations_causmore.yaml dataset ucdp_term_dyad",
        },
        notes="KG-only source (no atlas table). Shares ucdp.downloads with atlas.ucdp and atlas.ucdp_term.",
    )


def build(raw: Any, *, strict: bool = True) -> dict[str, Any]:
    sha = hashlib.sha256(raw.body).hexdigest()
    rows = parse(raw.body, strict=strict)
    out = build_kg(rows, sha=sha)
    cm.check_unique(out["entities"], out["statements"])
    return {**out, "sha": sha, "rows": len(rows)}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the dyad-level termination CSV and the codebook, archive, load the KG records, deliver the claims."""
    if not run.wants(DATASET):
        return {"skipped": True}
    raw = await run.get(DATA_URL, key=f"term_dyad:{TERM_VERSION}", media_types=MEDIA, max_bytes=MAX_BYTES)
    doc = cm.document(CODEBOOK_KEY)
    book = await run.get(doc["download_url"], key=CODEBOOK_KEY, media_types=PDF_MEDIA, max_bytes=MAX_BYTES)
    check = cm.check_pdf(CODEBOOK_KEY, book.body)
    out = build(raw, strict=run.origin == "live")
    run.archive(raw)
    run.archive(book)
    res: dict[str, Any] = {
        "rows": out["rows"],
        "episodes": out["episodes"],
        "anomalies": out["anomalies"],
        "codebook": {"sha256": check["sha256"], "matches": check["matches"]},
        "kg_counts": {"entities": len(out["entities"]), "statements": len(out["statements"])},
    }
    if cd.kg_available(run.engine):
        res["kg"] = cm.load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision="ucdp-term-dyad-v4-2024",
            raw_sha256s=[out["sha"]],
            retrieved_at=raw.retrieved_at,
            origin=run.origin,
        )
    if not check["matches"]:
        res["claims_withheld"] = {CLAIMS_DATASET: "codebook PDF differs from the pinned sha256"}
        res["claims"] = {}
        return res
    res["claims"] = cd.deliver_claims(
        run,
        [
            cm.delivery(
                CLAIMS_DATASET,
                claims=out["claims"],
                raw_sha256s=[out["sha"]],
                retrieved_at=raw.retrieved_at,
                origin=run.origin,
                documents_sha256={CODEBOOK_KEY: check["sha256"]},
            )
        ],
    )
    return res
