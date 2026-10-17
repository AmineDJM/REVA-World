"""WHO Model List of Essential Medicines (electronic EML, eEML) -> KG medicines, sections, indications (V6, V6DRUGS).

Source
------
WHO electronic Essential Medicines List, ``https://list.essentialmeds.org`` (current EML and EMLc as served by the
eEML; the site states no edition number, so the revision is the retrieval day plus the list page's sha256 prefix).
Licence (``/licencing``, checked 2026-10-17): **CC BY 3.0 IGO**, "you are free to copy, distribute, transmit, and
adapt this work, including for commercial purposes", except "In conjunction with the advertising or promotion of
commercial products, including proprietary medicines" and "In any manner that suggests that WHO endorses any
specific organisation or products"; required attribution "WHO electronic Essential Medicines List (eEML), World
Health Organization, 2020. https://list.essentialmeds.org/ (beta version 1.0). Licence: CC BY 3.0 IGO." and, for
an adaptation, WHO's disclaimer (both in :data:`RIGHTS`).

The eEML has no machine-readable export with identifiers: its XLSX export (POST ``/print``) carries names only (no
medicine id, no ICD-11 code, no DrugBank id), so the connector reads the HTML pages, which carry the identifiers:

1. the list page ``/`` -> medicine ids (``/medicines/<id>``) and names;
2. each medicine page ``/medicines/<id>`` -> ATC codes (WHOCC, flagged non-commercial), medicine type, EML status
   history (year, Technical Report Series number), therapeutic equivalence, DrugBank accession (link), the
   medicine's recommendation ids;
3. each recommendation page ``/recommendations/<id>`` -> section path (eEML section ids), EML / EMLc badges, list
   type, indication text with its ICD-11 MMS code and ICD entity URI, formulations, sex/age fields, therapeutic
   alternatives (medicine ids).

About 1,800 requests (~22 MB) per full run. Page structure is checked on every page (drift blocks the load).

KG (source key ``who.eeml``)
---------------------------
``eml:medicine:<id>`` (kind drug) ``--eml:recommended_for--> icd11mms:<code>`` (family associated_with: a WHO
listing for an indication, not an efficacy or causal claim), ``--eml:on_list--> eml:list:who_eml`` /
``eml:list:emlc`` (part_of), ``--eml:in_section--> eml:section:<id>`` (part_of; sections ``--eml:section_parent-->``
their parent section), ``--eml:therapeutic_alternative--> eml:medicine:<id>`` (context), literals ATC / type /
DrugBank / status history. ``--eml:chembl_molecule--> chembl:<id>`` (context) when exactly one approved ChEMBL drug
carries the medicine's DrugBank accession through UniChem (``unichem:drugbank_id``, same standard InChI): an
identifier bridge, never a name match; ambiguous or absent bridges are counted, not guessed.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from reva.adapters.connectors import drugs_common as dc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.who_eml"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_drugs_eml/1"
SOURCE_KEY = "who.eeml"
DATASET = "eml"
HOST = "list.essentialmeds.org"
BASE = f"https://{HOST}"
LIST_URL = f"{BASE}/"
MAX_BYTES = 6_000_000
MIN_MEDICINES_LIVE = 500
MEDIA = ("text/html",)
RIGHTS = {
    "licence": "CC BY 3.0 IGO",
    "licence_url": "https://creativecommons.org/licenses/by/3.0/igo/",
    "terms_url": "https://list.essentialmeds.org/licencing",
    "use_restrictions": "not in conjunction with the advertising or promotion of commercial products, including "
    "proprietary medicines; not in any manner that suggests that WHO endorses any specific organisation or products; "
    "no WHO logo",
    "attribution": "WHO electronic Essential Medicines List (eEML), World Health Organization, 2020. "
    "https://list.essentialmeds.org/ (beta version 1.0). Licence: CC BY 3.0 IGO.",
    "adaptation_disclaimer": "This is an adaptation of an original work by World Health Organization (WHO). Views "
    "and opinions expressed in the adaptation are the sole responsibility of the author or authors of the adaptation "
    "and are not endorsed by World Health Organization (WHO).",
}
#: Fixture mode: atropine, acetylsalicylic acid, imatinib, metformin, nicotine replacement therapy.
FIXTURE_MEDICINES = ("10", "13", "95", "191", "530")
LIST_EML = "eml:list:who_eml"
LIST_EMLC = "eml:list:emlc"

MED_LINK_RE = re.compile(r"^https?://list\.essentialmeds\.org/medicines/([0-9]{1,6})/?$")
REC_LINK_RE = re.compile(r"^https?://list\.essentialmeds\.org/recommendations/([0-9]{1,6})/?$")
SECTION_LINK_RE = re.compile(r"^/\?section=([0-9]{1,6})$")
ATC_RE = re.compile(r"^[A-Z][0-9]{2}[A-Z]{0,2}[0-9]{0,2}$")
DRUGBANK_RE = re.compile(r"^https?://(?:www\.)?drugbank\.(?:ca|com)/drugs/(DB[0-9]{5})/?$")
ICD_URI_RE = re.compile(r"(https?://id\.who\.int/icd/entity/[0-9]{1,12}(?:/mms(?:/[a-z]+)?)?)$")
ICD_CODE_RE = re.compile(r"^[0-9A-Z]{2}[0-9A-Z]{1,2}(?:\.[0-9A-Z]{1,4})?(?:&[0-9A-Z.]+)?$")
HISTORY_RE = re.compile(r"^(.*?)\s+in\s+([0-9]{4})\b(?:\s*\(TRS\s*([0-9]{1,5})\))?")
FOUND_RE = re.compile(r"Found\s+([0-9]+)\s+recommendations\s+for\s+([0-9]+)\s+medicines")


# ============================================================================================== parsing
@dataclass
class Medicine:
    id: str
    name: str
    atc: list[str] = field(default_factory=list)
    medicine_type: str | None = None
    drugbank: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    equivalent_for: list[tuple[str, str]] = field(default_factory=list)  # (medicine id, recommendation id)


@dataclass
class Recommendation:
    id: str
    medicine_id: str
    sections: list[tuple[str, str]]  # (section id, label) from the root to the leaf
    badges: list[str]
    indication: str | None
    icd_code: str | None
    icd_uri: str | None
    fields: dict[str, str]
    alternatives: list[str]
    history: list[dict[str, Any]]


def parse_list(body: bytes) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """``([(medicine id, name)], stated counts)`` of the list page."""
    root = dc.parse_html(body, "eEML list page")
    found = root.find(cls="found-text")
    stated: dict[str, int] = {}
    if found is not None:
        m = FOUND_RE.search(found.text())
        if m:
            stated = {"recommendations": int(m.group(1)), "medicines": int(m.group(2))}
    out: dict[str, str] = {}
    for h in root.find_all("h5", "medicine-name"):
        name = h.attrs.get("title", "").strip()
        ids = [MED_LINK_RE.match(a.attrs.get("href", "")) for a in h.find_all("a")]
        ids_ = [m.group(1) for m in ids if m]
        if not name or len(ids_) != 1:
            raise dc.err("eEML list page: medicine entry without a single /medicines/<id> link or title")
        out.setdefault(ids_[0], name)
    if not out:
        raise dc.err("eEML list page: no medicine entries (class medicine-name)")
    return sorted(out.items(), key=lambda x: int(x[0])), stated


def _rows(root: dc.Node) -> dict[str, dc.Node]:
    """``label -> content node`` of the ``header-row`` blocks (first occurrence of a label wins)."""
    out: dict[str, dc.Node] = {}
    for r in root.find_all("div", "header-row"):
        lab, con = r.find("div", "label-cell"), r.find("div", "content-cell")
        if lab is not None and con is not None:
            out.setdefault(lab.text(), con)
    return out


def _history(node: dc.Node | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if node is None:
        return out
    box = node.find("div", "status-history")
    if box is None:
        return out
    for d in box.children:
        if not isinstance(d, dc.Node) or d.tag != "div":
            continue
        txt = d.text()
        m = HISTORY_RE.match(txt)
        if not m:
            raise dc.err(f"eEML status history: unexpected entry {txt[:80]!r}")
        recs = [r.group(1) for a in d.find_all("a") if (r := REC_LINK_RE.match(a.attrs.get("href", "")))]
        out.append(
            {
                "event": m.group(1).strip(),
                "year": int(m.group(2)),
                "trs": m.group(3),
                "recommendation": recs[0] if recs else None,
                "text": txt[:400],
            }
        )
    return out


def parse_medicine(body: bytes, mid: str) -> Medicine:
    root = dc.parse_html(body, f"eEML medicine {mid}")
    title = root.find("div", "medicine-title")
    if title is None:
        raise dc.err(f"eEML medicine {mid}: no medicine-title")
    med = Medicine(id=mid, name=title.text() or title.attrs.get("title", ""))
    rows = _rows(root)
    if "ATC codes" in rows:
        for a in rows["ATC codes"].find_all("a"):
            code = a.text()
            if ATC_RE.match(code):
                med.atc.append(code)
    if "Medicine type" in rows:
        med.medicine_type = rows["Medicine type"].text() or None
    if "DrugBank" in rows:
        for a in rows["DrugBank"].find_all("a"):
            m = DRUGBANK_RE.match(a.attrs.get("href", ""))
            if m:
                med.drugbank = m.group(1)
    med.history = _history(rows.get("EML status history"))
    if "Therapeutic equivalent for" in rows:
        links = rows["Therapeutic equivalent for"].find_all("a")
        cur: str | None = None
        for a in links:
            href = a.attrs.get("href", "")
            if (m := MED_LINK_RE.match(href)) is not None:
                cur = m.group(1)
            elif (r := REC_LINK_RE.match(href)) is not None and cur is not None:
                med.equivalent_for.append((cur, r.group(1)))
                cur = None
    recs_box = root.find("div", "recommendations")
    if recs_box is None:
        raise dc.err(f"eEML medicine {mid}: no recommendations block")
    for a in recs_box.find_all("a", "indication-name"):
        m = REC_LINK_RE.match(a.attrs.get("href", ""))
        if m and m.group(1) not in med.recommendations:
            med.recommendations.append(m.group(1))
    return med


def parse_recommendation(body: bytes, rid: str) -> Recommendation:
    root = dc.parse_html(body, f"eEML recommendation {rid}")
    title = root.find("div", "medicine-title")
    mids = [
        m.group(1) for a in (title.find_all("a") if title else []) if (m := MED_LINK_RE.match(a.attrs.get("href", "")))
    ]
    if len(mids) != 1:
        raise dc.err(f"eEML recommendation {rid}: medicine link not found")
    sections: list[tuple[str, str]] = []
    for a in root.find_all("a", "section-link"):
        m = SECTION_LINK_RE.match(a.attrs.get("href", ""))
        if not m:
            raise dc.err(f"eEML recommendation {rid}: section link {a.attrs.get('href')!r}")
        sections.append((m.group(1), a.text()))
    badges = [
        d.text()
        for d in root.find_all("div", "decision-data-item")
        if "atc-code-container" not in d.classes and d.text()
    ]
    rows = _rows(root)
    ind = rows.get("Indication")
    indication = code = uri = None
    if ind is not None:
        span = ind.find("span")
        indication = span.text() if span is not None else ind.text()
        link = ind.find("a", "icd-link")
        if link is not None:
            c = link.text()
            code = c if ICD_CODE_RE.match(c) else None
            m = ICD_URI_RE.search(link.attrs.get("href", ""))
            uri = m.group(1) if m else None
    fields: dict[str, str] = {
        k: rows[k].text()[:1000]
        for k in (
            "INN",
            "Medicine type",
            "List type",
            "Additional notes",
            "Formulations",
            "Sex",
            "Age",
            "Age restriction",
            "Weight restriction",
            "Patent information",
        )
        if k in rows and rows[k].text()
    }
    alts: list[str] = []
    if "Therapeutic alternatives" in rows:
        for a in rows["Therapeutic alternatives"].find_all("a"):
            m = MED_LINK_RE.match(a.attrs.get("href", ""))
            if m and m.group(1) not in alts:
                alts.append(m.group(1))
    return Recommendation(
        id=rid,
        medicine_id=mids[0],
        sections=sections,
        badges=badges,
        indication=indication,
        icd_code=code,
        icd_uri=uri,
        fields=fields,
        alternatives=alts,
        history=_history(rows.get("EML status history")),
    )


# ============================================================================================== build
def build(
    medicines: Sequence[Medicine],
    recommendations: Sequence[Recommendation],
    raws: Mapping[str, str],
    *,
    revision: str,
    drugbank_to_chembl: Mapping[str, Sequence[str]] | None,
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """(entities, statements, report). ``raws``: ``medicine:<id>`` / ``recommendation:<id>`` -> raw sha256."""
    from reva.kg.model import KgEntity, KgStatement

    ents: dict[str, Any] = {}
    sts: list[Any] = []
    rep: Counter[str] = Counter()

    def ent(qid: str, kind: str, label: str, desc: str) -> None:
        if qid not in ents:
            ents[qid] = KgEntity(
                qid=qid,
                kind=kind,
                labels={"en": label} if label else {},
                descriptions={"en": desc},
                source_revision=revision,
            )

    def st(
        subject: str,
        pid: str,
        raw: str,
        sel: Mapping[str, Any],
        *,
        obj: str | None = None,
        value: Any = None,
        quals: Mapping[str, Any] | None = None,
        sid: str | None = None,
    ) -> None:
        sts.append(
            KgStatement(
                subject_qid=subject,
                property_pid=pid,
                object_qid=obj,
                value=value,
                qualifiers={k: v for k, v in (quals or {}).items() if v not in (None, "", [])},
                references=[{"stated_in": "WHO electronic Essential Medicines List (eEML)", "selector": dict(sel)}],
                statement_id=sid,
                source_revision=revision,
                raw_sha256=raw,
            )
        )

    ent(
        LIST_EML,
        "essential_medicines_list",
        "WHO Model List of Essential Medicines (as served by the eEML)",
        "REVA-composed key for the WHO Model List(s) of Essential Medicines published in the eEML",
    )
    ent(
        LIST_EMLC,
        "essential_medicines_list",
        "WHO Model List of Essential Medicines for Children (EMLc)",
        "REVA-composed key; a recommendation is linked to it only when its eEML page shows the 'EMLc' badge",
    )
    for med in medicines:
        q = f"eml:medicine:{med.id}"
        raw = raws[f"medicine:{med.id}"]
        url = f"{BASE}/medicines/{med.id}"
        ent(q, dc.KIND_DRUG, med.name, f"WHO eEML medicine {med.id} ({url})")
        sel = {"page": url}
        for code in med.atc:
            st(q, "eml:atc_code", raw, sel, value=dc.external_id(code), quals=dc.ATC_FLAGS)
        if med.medicine_type:
            st(q, "eml:medicine_type", raw, sel, value=dc.string_value(med.medicine_type))
        if med.drugbank:
            st(
                q,
                "eml:drugbank_id",
                raw,
                sel,
                value=dc.external_id(med.drugbank),
                quals={"note": "accession of the DrugBank link on the eEML page (DrugBank content not fetched)"},
            )
            rep["medicines_with_drugbank"] += 1
            if drugbank_to_chembl is not None:
                hits = sorted(set(drugbank_to_chembl.get(med.drugbank) or ()))
                if len(hits) == 1:
                    st(
                        q,
                        "eml:chembl_molecule",
                        raw,
                        sel,
                        obj=hits[0],
                        quals={
                            "via_drugbank": med.drugbank,
                            "basis": "eEML DrugBank link = UniChem DrugBank accession of this approved ChEMBL drug "
                            "(same standard InChI); identifier bridge (statement unichem:drugbank_id)",
                        },
                    )
                    rep["linked_to_chembl"] += 1
                elif hits:
                    rep["drugbank_maps_to_several_chembl_ids"] += 1
                else:
                    rep["drugbank_without_approved_chembl_drug"] += 1
        for i, h in enumerate(med.history):
            st(
                q,
                "eml:status_history",
                raw,
                sel,
                value=dc.year_value(h["year"]),
                quals={
                    "event": h["event"],
                    "trs": h["trs"],
                    "recommendation": h["recommendation"],
                    "text_as_written": h["text"],
                },
                sid=f"med:{med.id}:hist:{i}",
            )
        for other, rid in med.equivalent_for:
            st(
                f"eml:medicine:{other}",
                "eml:therapeutic_alternative",
                raw,
                sel,
                obj=q,
                quals={
                    "recommendation": rid,
                    "reading": f"medicine {med.id} is a therapeutic equivalent for "
                    f"medicine {other} (eEML 'Therapeutic equivalent for')",
                },
                sid=f"med:{med.id}:equiv:{other}:{rid}",
            )
    for r in recommendations:
        q = f"eml:medicine:{r.medicine_id}"
        raw = raws[f"recommendation:{r.id}"]
        url = f"{BASE}/recommendations/{r.id}"
        sel = {"page": url}
        base = {
            "recommendation": r.id,
            "indication_as_written": r.indication,
            "icd11_code": r.icd_code,
            "icd11_uri": r.icd_uri,
            "sections": [f"{sid_} {lab}" for sid_, lab in r.sections],
            "badges": r.badges,
            **{k.lower().replace(" ", "_"): v for k, v in r.fields.items()},
        }
        if r.icd_code:
            iq = f"icd11mms:{r.icd_code}"
            ent(
                iq,
                "icd11_category",
                r.indication or r.icd_code,
                f"ICD-11 MMS {r.icd_code}, labelled with the eEML indication text of recommendation {r.id}",
            )
            st(
                q,
                "eml:recommended_for",
                raw,
                sel,
                obj=iq,
                quals={
                    **base,
                    "note": "WHO essential-medicine listing "
                    "for this indication: an association, not an efficacy or causal statement",
                },
                sid=f"rec:{r.id}",
            )
            if r.icd_uri:
                st(iq, "eml:icd11_uri", raw, sel, value=dc.url_value(r.icd_uri), sid=f"rec:{r.id}:icd11uri")
            rep["recommendations_with_icd11"] += 1
        elif r.indication:
            st(
                q,
                "eml:indication_text",
                raw,
                sel,
                value=dc.string_value(r.indication),
                quals=base,
                sid=f"rec:{r.id}:text",
            )
            rep["recommendations_without_icd11"] += 1
        st(q, "eml:on_list", raw, sel, obj=LIST_EML, quals=base, sid=f"rec:{r.id}:list")
        if any(b.strip() == "EMLc" for b in r.badges):
            st(q, "eml:on_list", raw, sel, obj=LIST_EMLC, quals=base, sid=f"rec:{r.id}:emlc")
            rep["recommendations_emlc_badge"] += 1
        parent: str | None = None
        for sid_, lab in r.sections:
            sq = f"eml:section:{sid_}"
            ent(sq, "eml_section", lab, f"WHO eEML section {sid_} ({BASE}/?section={sid_})")
            if parent is not None:
                st(sq, "eml:section_parent", raw, sel, obj=parent, sid=f"section:{sid_}:parent:{parent}")
            parent = sq
        if parent is not None:
            st(q, "eml:in_section", raw, sel, obj=parent, quals={"recommendation": r.id}, sid=f"rec:{r.id}:section")
        for alt in r.alternatives:
            st(
                q,
                "eml:therapeutic_alternative",
                raw,
                sel,
                obj=f"eml:medicine:{alt}",
                quals={
                    "recommendation": r.id,
                    "reading": "therapeutic alternative listed on the recommendation (square box)",
                },
                sid=f"rec:{r.id}:alt:{alt}",
            )
    # deduplicate (a section parent edge is stated by every recommendation of the section: keep the first)
    seen: set[tuple[str, str | None, str | None]] = set()
    uniq = []
    for s in sts:
        k = (s.subject_qid + "|" + s.property_pid, s.object_qid, s.statement_id)
        if s.statement_id and k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    rep_out = {
        "medicines": len(medicines),
        "recommendations": len(recommendations),
        "sections": sum(1 for e in ents.values() if e.kind == "eml_section"),
        "icd11_categories": sum(1 for e in ents.values() if e.kind == "icd11_category"),
        "statements": len(uniq),
        **dict(rep),
    }
    return list(ents.values()), uniq, rep_out


# ============================================================================================== run
def code_digest() -> str:
    import sys

    return dc.digest(sys.modules[__name__], dc)


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source="eeml",
        provider="World Health Organization, electronic Essential Medicines List (eEML)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); {RIGHTS['attribution']}; restrictions: "
        f"{RIGHTS['use_restrictions']}"[:500],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=(LIST_URL, f"{BASE}/medicines/<id>", f"{BASE}/recommendations/<id>"),
        cadence_seconds=180 * 86400,
        planned_requests=2000,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={
            "medicine": [
                "ATC codes",
                "Medicine type",
                "EML status history",
                "DrugBank",
                "Therapeutic equivalent for",
                "recommendations",
            ],
            "recommendation": [
                "section-link",
                "decision-data-item",
                "Indication (ICD11)",
                "List type",
                "Formulations",
                "Sex",
                "Age",
                "Therapeutic alternatives",
            ],
        },
        notes="KG only: eEML medicines, recommendations (indication with ICD-11 code), sections, DrugBank bridge.",
    )


async def _page(run: Any, url: str, key: str) -> tuple[bytes, str, Any]:
    raw = await run.get(url, key=key, media_types=MEDIA, max_bytes=MAX_BYTES)
    sha = run.archive(raw)
    return run.store.get("raw", sha), sha, raw


async def atlas_ingest(run: Any) -> dict[str, Any]:
    if not run.wants(DATASET):
        return {"skipped": True}
    only = dc.opt_list(run.options.get("medicines"))
    if only is None and run.origin == "test_fixture":
        only = list(FIXTURE_MEDICINES)
    partial = only is not None and run.origin == "live"
    body, list_sha, lraw = await _page(run, LIST_URL, "eeml:list")
    listed, stated = parse_list(body)
    names = dict(listed)
    if only is not None:
        unknown = [m for m in only if m not in names]
        if unknown:
            raise dc.err(f"option medicines: not on the list page: {unknown[:10]}", kind="invalid_option")
        ids = [m for m in only]
    else:
        ids = [m for m, _ in listed]
        if run.origin == "live" and len(ids) < MIN_MEDICINES_LIVE:
            raise dc.err(f"eEML list page: only {len(ids)} medicines: truncated?", kind="truncated")
    raws: dict[str, str] = {}
    meds: list[Medicine] = []
    recs: list[Recommendation] = []
    for mid in ids:
        b, sha, _ = await _page(run, f"{BASE}/medicines/{mid}", f"eeml:medicine:{mid}")
        raws[f"medicine:{mid}"] = sha
        meds.append(parse_medicine(b, mid))
    for med in meds:
        for rid in med.recommendations:
            if f"recommendation:{rid}" in raws:
                continue
            b, sha, _ = await _page(run, f"{BASE}/recommendations/{rid}", f"eeml:recommendation:{rid}")
            raws[f"recommendation:{rid}"] = sha
            rec = parse_recommendation(b, rid)
            if rec.medicine_id != med.id:
                raise dc.err(f"eEML recommendation {rid} belongs to medicine {rec.medicine_id}, listed under {med.id}")
            recs.append(rec)
    retrieved: datetime = lraw.retrieved_at
    revision = f"eEML {retrieved.date().isoformat()} list {list_sha[:12]}"
    out: dict[str, Any] = {
        "revision": revision,
        "stated_on_list_page": stated,
        "medicines_listed": len(listed),
        "partial": partial,
    }
    if not dc.kg_available(run.engine):
        ents, sts, rep = build(meds, recs, raws, revision=revision, drugbank_to_chembl=None)
        out.update(build=rep, kg={"status": "kg_unavailable"})
        return out
    db2chembl: defaultdict[str, list[str]] = defaultdict(list)
    for subj, val in dc.current_literals(run.engine, run.origin, "unichem:drugbank_id"):
        db2chembl[val].append(subj)
    ents, sts, rep = build(meds, recs, raws, revision=revision, drugbank_to_chembl=db2chembl)
    rep["unichem_drugbank_rows_in_kg"] = sum(len(v) for v in db2chembl.values())
    out["build"] = rep
    out["kg"] = dc.load_kg(
        run.engine,
        run.store,
        entities=ents,
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=sorted({list_sha, *raws.values()}),
        retrieved_at=retrieved,
        origin=run.origin,
        parameters={"connector": NAME, "parser": PARSER_VERSION, "partial": partial, "medicines": len(meds)},
        prefixes=("eml:",),
        retract=not partial,
    )
    return out


__all__ = [
    "NAME",
    "SOURCE_KEY",
    "Medicine",
    "Recommendation",
    "atlas_ingest",
    "atlas_source",
    "build",
    "parse_list",
    "parse_medicine",
    "parse_recommendation",
]
