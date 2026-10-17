"""PA-X Peace Agreements Database v10 (University of Edinburgh, PeaceRep): peace agreements as events, the conflict or
war each one addresses (T1 ``reaction_to`` claims), peace processes (context) and UCDP agreement ids (identity
evidence) (agent CAUSMORE).

Source and access
-----------------
``www.peaceagreements.org`` (no account): the full dataset ``/cms/documents/3957/pax_data_2257_agreements_v10.csv``
(2,257 agreements 1990-2025, one row per agreement) and the codebook ``/cms/documents/3956/PA_X_codebook_v10.pdf``
(sha256 pinned in ``registry/causal/relations_causmore.yaml``: a changed codebook withholds the claims). Registered
source ``pax.peaceagreements``; 2 requests per run.

**Terms** (Terms_of_Use.pdf, read 2026-10-17): "the content of the Databases is provided under a Creative Commons
Attribution-NonCommercial-ShareAlike 4.0 International Licence (CC BY-NC-SA 4.0)"; "You may not charge a fee ...
commercial use is strictly prohibited"; data use "should be notified to peacerep@ed.ac.uk". Non-commercial: flagged
for maintainer review like CShapes / JST.

What is built (reasons: ``relations_causmore.yaml`` dataset ``pax``)
-------------
* ``pax:agt:<AgtId>`` (peace agreement; ``causmore:event_type`` = ``peace_agreement``): ``P585`` signature date
  (``Dat``), stage / status / agreement type verbatim, the conflict type as ``causdata:coded_issue`` (an issue, not a
  cause), parties as written, ``P17`` countries (``Loc1ISO``/``Loc2ISO`` -> crosswalk QIDs; unmapped codes kept as
  ``pax:location`` strings), ``P361`` part of ``pax:process:<PP>`` (peace process, ``PPName``).
* Identifiers: ``pax:ucdp_agreement_id`` (``UcdpAgr``; ``same_as`` to ``ucdp:pa:<id>`` through
  ``registry/events/same_as_rules.yaml`` rule ``pax_ucdp_agreement_id``), ``pax:pam_agreement_id`` (``PamAgr``).
* T1 claims ``reaction_to``: ``ucdp:conflict:<UcdpCon>`` -> agreement and ``cow:war:<CowWar>`` -> agreement (codebook
  term ``peace_agreement``: "addressing conflict with a view to ending it"), with ``pax:ucdp_conflict`` /
  ``pax:cow_war`` context statements carrying the same pairing. ``CowWar`` is the v4 COW numbering (see
  :mod:`atlas_cowwars`).
* Not made: links between agreements. ``Stage = Imp`` ("Aiming to implement an earlier agreement") names no agreement
  and ``OthAgr`` lists other agreements by their names "as found in the text": joining them would be name matching.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors import causmore_common as cm
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement

NAME = "atlas.pax"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_pax/1"
SOURCE_KEY = "pax.peaceagreements"
ATLAS_SOURCE = "pax"
DATASET = "agreements_kg"
CLAIMS_DATASET = "pax"
HOST = "www.peaceagreements.org"
DATA_URL = f"https://{HOST}/cms/documents/3957/pax_data_2257_agreements_v10.csv"
DATA_FILE = "pax_data_2257_agreements_v10.csv"
CODEBOOK_KEY = "pax_codebook"
PAX_VERSION = "v10"
MEDIA = ("text/csv", "application/csv", "application/octet-stream", "text/plain")
PDF_MEDIA = ("application/pdf", "application/octet-stream")
MAX_BYTES = 12_000_000
MIN_ROWS = 2000
REQUIRED = (
    "Con", "Contp", "PP", "PPName", "Reg", "AgtId", "Ver", "Agt", "Dat", "Status", "Agtp", "Stage", "StageSub",
    "Part", "ThrdPart", "OthAgr", "Loc1ISO", "Loc2ISO", "UcdpCon", "UcdpAgr", "PamAgr", "CowWar",
)  # fmt: skip
NOT_CODED = ("", "NA", "N/A", "-8", "-9", "n/a", "na")

RIGHTS: dict[str, Any] = {
    "licence": "CC BY-NC-SA 4.0 (PA-X terms of use: non-commercial, no fee; notify peacerep@ed.ac.uk of data use)",
    "licence_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    "terms_url": "https://www.peaceagreements.org/media/documents/Terms_of_Use.pdf",
    "open_licence": False,
    "attribution": "Bell, C., & Badanjak, S. (2019). Introducing PA-X: A new peace agreement database and dataset. "
    "Journal of Peace Research, 56(3), 452-466. PA-X Peace Agreements Database v10, PeaceRep, University of "
    "Edinburgh, https://www.peaceagreements.org/",
}


def parse(body: bytes, *, strict: bool = True) -> list[dict[str, str]]:
    rows = cd.read_csv_bytes(body, name=DATA_FILE, required=REQUIRED)
    for i, r in enumerate(rows):
        r["_line"] = str(i + 2)
    if strict and len(rows) < MIN_ROWS:
        raise cd.drift(f"{DATA_FILE}: {len(rows)} rows < {MIN_ROWS} (truncated?)")
    ids = Counter(r["AgtId"] for r in rows)
    dup = [k for k, v in ids.items() if v > 1]
    if dup:
        raise cd.drift(f"{DATA_FILE}: duplicate AgtId {dup[:5]}")
    return rows


def coded_ids(text: Any) -> list[str]:
    """Integer ids of a coded field (``"289"``, ``"289, 290"``); NA / N/A / empty -> none."""
    out = []
    for part in str(text or "").replace(";", ",").split(","):
        s = part.strip()
        if s in NOT_CODED:
            continue
        try:
            f = float(s)
        except ValueError:
            continue
        if f > 0:
            out.append(str(int(f)) if f == int(f) else s)
    return out


def build_kg(
    rows: Sequence[Mapping[str, str]], *, sha: str, country_qids: Mapping[str, str]
) -> dict[str, Any]:
    from reva.adapters.connectors import atlas_cowwars as W

    ents: dict[str, KgEntity] = {}
    sts: list[KgStatement] = []
    claims: list[cd.ClaimRecord] = []
    anomalies: dict[str, list[Any]] = defaultdict(list)
    processes: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        agt = r["AgtId"].strip()
        if not agt.isdigit():
            anomalies["bad_agreement_id"].append(r["_line"])
            continue
        aid = f"pax:agt:{agt}"
        line = int(r["_line"])
        sel = {"file": DATA_FILE, "line": line, "AgtId": int(agt)}
        p = f"a{agt}"
        ents[aid] = KgEntity(
            qid=aid,
            kind="peace_agreement",
            labels={"en": r["Agt"] or f"PA-X agreement {agt}"},
            descriptions={"en": f"Peace agreement {agt} (PA-X {PAX_VERSION}), {r['Con']}, signed {r['Dat']}"},
        )
        sts.append(cm.string_stmt(f"{p}:type", aid, "causmore:event_type", "peace_agreement", raw_sha256=sha,
                                  selector=sel))
        iso = cd.iso_date(r["Dat"])
        if iso:
            sts.extend(cd.event_times(aid, f"{p}:t", point=(iso, 11), raw_sha256=sha,
                                      selector={**sel, "column": "Dat"},
                                      qualifiers={"definition": "The date on which the agreement was signed or "
                                                  "agreed."}))
        else:
            anomalies["undated"].append(agt)
        for col, pid in (("Stage", "pax:stage"), ("Status", "pax:status"), ("Agtp", "pax:agreement_type")):
            if r[col]:
                q = {"stage_sub": r["StageSub"] or None} if col == "Stage" else {}
                sts.append(cm.string_stmt(f"{p}:{col.lower()}", aid, pid, r[col], raw_sha256=sha,
                                          selector={**sel, "column": col}, variable=col, **q))
        if r["Contp"]:
            sts.append(cm.string_stmt(f"{p}:issue", aid, "causdata:coded_issue", r["Contp"], raw_sha256=sha,
                                      selector={**sel, "column": "Contp"}, variable="Contp",
                                      note="type of conflict the agreement is addressing: a coded issue, not a cause"))
        if r["Part"]:
            sts.append(cm.string_stmt(f"{p}:parties", aid, "pax:parties", r["Part"], raw_sha256=sha,
                                      selector={**sel, "column": "Part"}))
        for col in ("Loc1ISO", "Loc2ISO"):
            code = r[col].strip().upper()
            if not code or code in ("NA", "N/A"):
                continue
            q = country_qids.get(code)
            if q:
                sts.append(cm.item_stmt(f"{p}:{col.lower()}", aid, "P17", q, raw_sha256=sha,
                                        selector={**sel, "column": col}, iso3=code))
            else:
                sts.append(cm.string_stmt(f"{p}:{col.lower()}", aid, "pax:location", code, raw_sha256=sha,
                                          selector={**sel, "column": col}, note="ISO code not in the crosswalk"))
        pp = r["PP"].strip()
        if pp and pp not in NOT_CODED:
            processes[pp][r["PPName"]] += 1
            sts.append(cm.item_stmt(f"{p}:process", aid, "P361", f"pax:process:{pp}", raw_sha256=sha,
                                    selector={**sel, "columns": ["PP", "PPName"]}))
        for col, pid in (("UcdpAgr", "pax:ucdp_agreement_id"), ("PamAgr", "pax:pam_agreement_id")):
            for j, v in enumerate(coded_ids(r[col])):
                sts.append(cm.string_stmt(f"{p}:{col.lower()}:{j}", aid, pid, v, raw_sha256=sha,
                                          selector={**sel, "column": col}, variable=col))
        for cid in coded_ids(r["UcdpCon"]):
            target = f"ucdp:conflict:{cid}"
            sts.append(cm.item_stmt(f"{p}:ucdpcon:{cid}", aid, "pax:ucdp_conflict", target, raw_sha256=sha,
                                    selector={**sel, "column": "UcdpCon"}))
            claims.append(_claim(target, aid, sel, sha, "UcdpCon", f"{agt}:ucdp:{cid}"))
        for col_v in coded_ids(r["CowWar"]):
            n = W.war_number(col_v)
            if n is None:
                continue
            target = f"cow:war:{n}"
            sts.append(cm.item_stmt(f"{p}:cowwar:{n}", aid, "pax:cow_war", target, raw_sha256=sha,
                                    selector={**sel, "column": "CowWar"},
                                    numbering="COW v4 (Resort to War, Sarkees and Wayman 2010)"))
            claims.append(_claim(target, aid, sel, sha, "CowWar", f"{agt}:cow:{n}"))
    for pp, names in sorted(processes.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        name, _ = names.most_common(1)[0]
        pid = f"pax:process:{pp}"
        ents[pid] = KgEntity(
            qid=pid,
            kind="peace_process",
            labels={"en": name or f"PA-X peace process {pp}"},
            descriptions={"en": f"Peace or transition process {pp} (PA-X {PAX_VERSION})"},
        )
        sts.append(cm.string_stmt(f"pp{pp}:type", pid, "causmore:event_type", "peace_process", raw_sha256=sha,
                                  selector={"file": DATA_FILE, "columns": ["PP", "PPName"], "PP": pp}))
        if len(names) > 1:
            anomalies["process_with_several_names"].append({"PP": pp, "names": sorted(names)})
    return {"entities": list(ents.values()), "statements": sts, "claims": claims, "anomalies": dict(anomalies)}


def _claim(cause: str, effect: str, sel: Mapping[str, Any], sha: str, col: str, scid: str) -> cd.ClaimRecord:
    return cd.ClaimRecord(
        cause_id=cause,
        effect_id=effect,
        relation="reaction_to",
        selector={**sel, "columns": ["AgtId", col, "Dat"]},
        raw_sha256=sha,
        codebook_term="peace_agreement",
        source_claim_id=scid,
        scope={"coded_by": col},
    )


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="PA-X Peace Agreements Database (PeaceRep, University of Edinburgh)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=(f"pax-{PAX_VERSION}",),
        cadence_seconds=90 * 86400,
        planned_requests=3,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "kg": "pax:agt:<AgtId>, pax:process:<PP>",
            "claims": "registry/causal/relations_causmore.yaml dataset pax (UcdpCon, CowWar -> reaction_to)",
            "same_as": "pax:ucdp_agreement_id -> ucdp:pa:<id> (registry/events/same_as_rules.yaml)",
        },
        notes="KG-only source (no atlas table).",
    )


def build(raw: Any, *, country_qids: Mapping[str, str], strict: bool = True) -> dict[str, Any]:
    sha = hashlib.sha256(raw.body).hexdigest()
    rows = parse(raw.body, strict=strict)
    out = build_kg(rows, sha=sha, country_qids=country_qids)
    cm.check_unique(out["entities"], out["statements"])
    return {**out, "sha": sha, "rows": len(rows)}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the PA-X CSV and codebook, archive, load the KG records, deliver the claims."""
    if not run.wants(DATASET):
        return {"skipped": True}
    raw = await run.get(DATA_URL, key=f"pax:{PAX_VERSION}", media_types=MEDIA, max_bytes=MAX_BYTES)
    doc = cm.document(CODEBOOK_KEY)
    book = await run.get(doc["download_url"], key=CODEBOOK_KEY, media_types=PDF_MEDIA, max_bytes=MAX_BYTES)
    check = cm.check_pdf(CODEBOOK_KEY, book.body)
    qids = cm.country_qids()
    out = build(raw, country_qids=qids, strict=run.origin == "live")
    run.archive(raw)
    run.archive(book)
    res: dict[str, Any] = {
        "rows": out["rows"],
        "anomalies": {k: (v if len(v) <= 30 else [*v[:30], f"... {len(v)} in total"]) for k, v in
                      out["anomalies"].items()},
        "codebook": {"sha256": check["sha256"], "matches": check["matches"]},
        "kg_counts": {"entities": len(out["entities"]), "statements": len(out["statements"])},
        "country_qids": len(qids),
    }
    if cd.kg_available(run.engine):
        res["kg"] = cm.load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision=f"pax-{PAX_VERSION}",
            raw_sha256s=[out["sha"]],
            retrieved_at=raw.retrieved_at,
            origin=run.origin,
        )
    if not check["matches"]:
        res["claims_withheld"] = {CLAIMS_DATASET: "codebook PDF differs from the pinned sha256"}
        res["claims"] = {}
        return res
    res["claims"] = cd.deliver_claims(run, [cm.delivery(
        CLAIMS_DATASET, claims=out["claims"], raw_sha256s=[out["sha"]], retrieved_at=raw.retrieved_at,
        origin=run.origin, documents_sha256={CODEBOOK_KEY: check["sha256"]})])
    return res
