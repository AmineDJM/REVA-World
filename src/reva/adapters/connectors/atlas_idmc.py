"""IDMC Global Internal Displacement Database (GIDD) - Disasters: disaster events and the internal displacements the
source attributes to them (T1 ``has_effect`` claims) (agent CAUSMORE).

Source and access
-----------------
Humanitarian Data Exchange (HDX), dataset ``idmc_internal_displacement_conflict-violence_disasters`` of the
organisation ``international-displacement-monitoring-centre-idmc`` (no account): one ``package_show`` request (the
dataset record: licence id, organisation, resource list) then the resource
``IDMC_GIDD_Disasters_Internal_Displacement_Data.xlsx`` it names (HDX redirects the download to its S3 file store,
``s3.us-east-1.amazonaws.com``). IDMC's own API (helix-tools-api) needs a registered client and is not used.
Registered source ``idmc.gidd``; 2 requests per run.

**Licence.** HDX's record says ``cc-by``; the workbook's own README sheet says "This content is licensed under CC
BY-NC". REVA applies the more restrictive statement of the provider's file (CC BY-NC 4.0, non-commercial) and keeps
both in the rights record (flagged for maintainer review).

**Codebook.** The README sheet of the workbook defines the fields. Its definitions quoted in
``registry/causal/relations_causmore.yaml`` (dataset ``idmc_disasters``) must each be a README cell verbatim, else
the claims are withheld (the workbook's bytes change with every data release, so no file sha256 is pinned).

What is built
-------------
* ``idmc:event:<Event ID>`` -- the disaster event (one per IDMC Event ID; a row without an Event ID is its own event
  ``idmc:event:r<sha1 of the row's ISO3|Year|Event Name|Date|Hazard Sub Type|raw figure|event codes>`` -- rows are
  never grouped by name; fully identical rows are reported, not loaded twice):
  label = the event name (most frequent over its rows), ``P580`` = "Date of Event (start)" ("Approximate start date of
  the event", kept at day precision with that caveat in the qualifiers), ``idmc:hazard_sub_type`` (CRED EM-DAT
  classification as IDMC gives it, category and type in the qualifiers), ``P17`` countries of its rows, GLIDE numbers
  and other event codes (identifiers, never causal).
* ``idmc:displacement:<event key>:<ISO3>:<Year>`` -- the internal displacements reported for that event, country and
  year (``causmore:event_type`` = ``disaster_displacement``): ``P585`` year, ``idmc:internal_displacements`` = the raw
  (not rounded) figure, the rounded figure and the "Displacement occurred" (preventive evacuations) value in the
  qualifiers, ``P17``.
* T1 claim ``has_effect`` (polarity ``promotes``) event -> displacement record, codebook term
  ``disaster_displacement``: the README defines the figures as internal displacements "as a result of disasters" and
  disaster displacement as people "forced to leave their homes ... as a result, or in anticipation of the negative
  impact of natural hazards". The preventive-evacuation flag travels in the claim scope.
* No atlas table (IDMC national totals are not re-tabulated here); the declared dataset ``disasters_kg`` gates the
  KG load.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors import causmore_common as cm
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement

NAME = "atlas.idmc"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_idmc/1"
SOURCE_KEY = "idmc.gidd"
ATLAS_SOURCE = "idmc"
DATASET = "disasters_kg"
CLAIMS_DATASET = "idmc_disasters"
HDX_HOST = "data.humdata.org"
S3_HOST = "s3.us-east-1.amazonaws.com"  # HDX resource downloads answer 302 -> its S3 file store
PACKAGE = "idmc_internal_displacement_conflict-violence_disasters"
PACKAGE_URL = f"https://{HDX_HOST}/api/3/action/package_show?id={PACKAGE}"
EXPECTED_ORGANISATION = "international-displacement-monitoring-centre-idmc"
RESOURCE_NAME = "IDMC_GIDD_Disasters_Internal_Displacement_Data.xlsx"
DATA_SHEET = "1_Disaster_Displacement_data"
README_SHEET = "README"
XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
    "binary/octet-stream",
    "application/zip",
)
MAX_BYTES = 40_000_000
MIN_ROWS = 15_000
COLUMNS = (
    "ISO3", "Country / Territory", "Year", "Event Name", "Date of Event (start)", "Disaster Internal Displacements",
    "Disaster Internal Displacements (Raw)", "Hazard Category", "Hazard Type", "Hazard Sub Type",
    "Event Codes (Code:Type)", "Event ID", "Displacement occurred",
)  # fmt: skip
EXCEL_EPOCH = date(1899, 12, 30)

RIGHTS: dict[str, Any] = {
    "licence": "CC BY-NC 4.0 (stated in the workbook README; HDX's dataset record says CC BY - the more restrictive "
    "provider statement is applied)",
    "licence_url": "https://creativecommons.org/licenses/by-nc/4.0/",
    "open_licence": False,
    "attribution": "Internal Displacement Monitoring Centre (IDMC). Global Internal Displacement Database - Disasters. "
    "https://www.internal-displacement.org/database/displacement-data/ (via HDX)",
}


def _err(msg: str, kind: str = "schema_drift") -> cd.CausdataError:
    return cd.CausdataError(msg, retryable=False, kind=kind)


def resource_url(package_body: bytes) -> tuple[str, dict[str, Any]]:
    """The disaster workbook's download URL from HDX ``package_show``, after checking organisation and resource."""
    try:
        doc = json.loads(package_body)
        res = doc["result"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err("HDX package_show: not a package record") from e
    org = (res.get("organization") or {}).get("name")
    if not doc.get("success") or res.get("name") != PACKAGE or org != EXPECTED_ORGANISATION:
        raise _err(f"HDX package_show: unexpected package/organisation {res.get('name')!r} / {org!r}")
    hits = [r for r in res.get("resources") or [] if r.get("name") == RESOURCE_NAME]
    if len(hits) != 1 or not str(hits[0].get("url", "")).startswith(f"https://{HDX_HOST}/"):
        raise _err(f"HDX package_show: resource {RESOURCE_NAME} not found once with an HDX URL")
    meta = {
        "hdx_license_id": res.get("license_id"),
        "hdx_license_title": res.get("license_title"),
        "organisation": org,
        "resource_id": hits[0].get("id"),
        "resource_last_modified": hits[0].get("last_modified"),
        "dataset_date": res.get("dataset_date"),
    }
    return str(hits[0]["url"]), meta


def parse(body: bytes, *, strict: bool = True) -> tuple[list[dict[str, str]], list[str]]:
    """(data rows, README cell texts)."""
    from reva.adapters.connectors.atlas_xlsx import Workbook, XlsxError

    try:
        wb = Workbook(body)
        table = wb.table(DATA_SHEET)
        readme = wb.table(README_SHEET)
    except (XlsxError, KeyError) as e:
        raise _err(f"{RESOURCE_NAME}: unreadable workbook or missing sheet") from e
    header = [str(h or "").strip() for h in table[0]] if table else []
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise _err(f"{RESOURCE_NAME}: missing columns {missing}")
    rows = []
    for i, r in enumerate(table[1:]):
        if not any(v not in (None, "") for v in r):
            continue
        d = {h: ("" if v is None else str(v).strip()) for h, v in zip(header, r, strict=False) if h}
        d["_row"] = str(i + 2)
        rows.append(d)
    if strict and len(rows) < MIN_ROWS:
        raise _err(f"{RESOURCE_NAME}: {len(rows)} rows < {MIN_ROWS} (truncated?)", kind="truncated")
    texts = [str(v) for r in readme for v in r if v not in (None, "")]
    return rows, texts


def excel_date(v: Any) -> str | None:
    s = str(v or "").strip()
    try:
        f = float(s)
    except ValueError:
        return cd.iso_date(s)
    if not 1 <= f < 2_958_466:
        return None
    return (EXCEL_EPOCH + timedelta(days=int(f))).isoformat()


def event_key(r: Mapping[str, str]) -> str:
    eid = r["Event ID"].strip()
    if eid and eid.replace(".0", "").isdigit():
        return eid.replace(".0", "")
    basis = "|".join(r[c] for c in ("ISO3", "Year", "Event Name", "Date of Event (start)", "Hazard Sub Type",
                                    "Disaster Internal Displacements (Raw)", "Event Codes (Code:Type)"))
    return "r" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def event_codes(text: Any) -> list[tuple[str, str]]:
    """``"FL-2016-000064-CHN:Glide Number; MDRIN028:IFRC Appeal ID"`` -> [(code, type)]."""
    out = []
    for part in str(text or "").split(";"):
        s = part.strip()
        if not s or s == "None":
            continue
        code, _, typ = s.rpartition(":")
        if not code:
            code, typ = s, ""
        out.append((code.strip(), typ.strip()))
    return out


def _amount(v: Any) -> str | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f != f or f < 0:
        return None
    return str(int(f)) if f == int(f) else s


def build_kg(rows: Sequence[Mapping[str, str]], *, sha: str, country_qids: Mapping[str, str]) -> dict[str, Any]:
    ents: dict[str, KgEntity] = {}
    sts: list[KgStatement] = []
    claims: list[cd.ClaimRecord] = []
    anomalies: dict[str, list[Any]] = defaultdict(list)
    by_event: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for r in rows:
        by_event[event_key(r)].append(r)
    for key, erows in sorted(by_event.items()):
        eid = f"idmc:event:{key}"
        first = erows[0]
        lines = [int(r["_row"]) for r in erows]
        sel = {"file": RESOURCE_NAME, "sheet": DATA_SHEET, "rows": lines[:50], "event_key": key}
        names = Counter(r["Event Name"] for r in erows if r["Event Name"])
        name = names.most_common(1)[0][0] if names else f"IDMC disaster event {key}"
        sub = first["Hazard Sub Type"]
        ents[eid] = KgEntity(
            qid=eid,
            kind="disaster_event",
            labels={"en": name},
            descriptions={"en": f"Disaster event ({sub or first['Hazard Type'] or 'hazard not given'}) in the IDMC "
                                f"Global Internal Displacement Database"
                                + ("" if key.startswith("r") else f", IDMC Event ID {key}")},
        )
        if len({(r["Hazard Category"], r["Hazard Type"], r["Hazard Sub Type"]) for r in erows}) > 1:
            anomalies["event_with_several_hazards"].append(key)
        if sub:
            sts.append(cm.string_stmt(f"e{key}:hazard", eid, "idmc:hazard_sub_type", sub, raw_sha256=sha,
                                      selector={**sel, "columns": ["Hazard Category", "Hazard Type",
                                                                   "Hazard Sub Type"]},
                                      hazard_category=first["Hazard Category"] or None,
                                      hazard_type=first["Hazard Type"] or None))
        dates = {excel_date(r["Date of Event (start)"]) for r in erows} - {None}
        if len(dates) > 1:
            anomalies["event_with_several_start_dates"].append(key)
        if dates:
            sts.extend(cd.event_times(eid, f"e{key}:t", start=(min(dates), 11), raw_sha256=sha,  # type: ignore[type-var]
                                      selector={**sel, "column": "Date of Event (start)"},
                                      qualifiers={"definition": "Date of Event (Start): Approximate start date of the "
                                                  "event.", "approximate": True}))
        for iso in sorted({r["ISO3"] for r in erows if r["ISO3"]}):
            q = country_qids.get(iso)
            if q:
                sts.append(cm.item_stmt(f"e{key}:country:{iso}", eid, "P17", q, raw_sha256=sha,
                                        selector={**sel, "column": "ISO3"}, iso3=iso))
            else:
                sts.append(cm.string_stmt(f"e{key}:country:{iso}", eid, "idmc:country", iso, raw_sha256=sha,
                                          selector={**sel, "column": "ISO3"}, note="ISO3 not in the crosswalk"))
        codes = sorted({c for r in erows for c in event_codes(r["Event Codes (Code:Type)"])})
        for j, (code, typ) in enumerate(codes):
            pid = "idmc:glide_number" if typ.lower() == "glide number" else "idmc:event_code"
            sts.append(cm.string_stmt(f"e{key}:code:{j}", eid, pid, code, raw_sha256=sha,
                                      selector={**sel, "column": "Event Codes (Code:Type)"}, code_type=typ or None))
        for r in erows:
            iso, year = r["ISO3"], r["Year"]
            if not iso or not year.isdigit():
                anomalies["row_without_country_or_year"].append(r["_row"])
                continue
            did = f"idmc:displacement:{key}:{iso}:{year}"
            rsel = {"file": RESOURCE_NAME, "sheet": DATA_SHEET, "row": int(r["_row"]), "event_key": key,
                    "ISO3": iso, "Year": int(year)}
            if did in ents:
                anomalies["duplicate_event_country_year"].append(did)
                continue
            raw_n = _amount(r["Disaster Internal Displacements (Raw)"])
            ents[did] = KgEntity(
                qid=did,
                kind="disaster_displacement",
                labels={"en": f"Internal displacements in {r['Country / Territory'] or iso} ({year}): {name}"},
                descriptions={"en": f"Disaster internal displacements reported by IDMC for {iso} in {year}"
                                    + (f" ({raw_n})" if raw_n else "")},
            )
            sts.append(cm.string_stmt(f"d{key}:{iso}:{year}:type", did, "causmore:event_type",
                                      "disaster_displacement", raw_sha256=sha, selector=rsel))
            sts.extend(cd.event_times(did, f"d{key}:{iso}:{year}:t", point=(f"{int(year):04d}-01-01", 9),
                                      raw_sha256=sha, selector={**rsel, "column": "Year"},
                                      qualifiers={"definition": "Year: Indicates the year for which displacement "
                                                  "data are reported."}))
            q = country_qids.get(iso)
            if q:
                sts.append(cm.item_stmt(f"d{key}:{iso}:{year}:country", did, "P17", q, raw_sha256=sha,
                                        selector={**rsel, "column": "ISO3"}, iso3=iso))
            else:
                sts.append(cm.string_stmt(f"d{key}:{iso}:{year}:country", did, "idmc:country", iso, raw_sha256=sha,
                                          selector={**rsel, "column": "ISO3"}))
            occurred = r["Displacement occurred"] or None
            if raw_n is not None:
                sts.append(KgStatement(
                    key=f"d{key}:{iso}:{year}:n",
                    subject=did,
                    property="idmc:internal_displacements",
                    value={"type": "quantity", "amount": raw_n, "unit": "internal displacements"},
                    qualifiers={k: v for k, v in {
                        "column": "Disaster Internal Displacements (Raw)",
                        "rounded": _amount(r["Disaster Internal Displacements"]),
                        "displacement_occurred": occurred,
                    }.items() if v is not None},
                    raw_sha256=sha,
                    selector={**rsel, "column": "Disaster Internal Displacements (Raw)"},
                ))
            if raw_n is None or float(raw_n) <= 0:
                anomalies["no_positive_figure"].append(r["_row"])
                continue
            claims.append(cd.ClaimRecord(
                cause_id=eid,
                effect_id=did,
                relation="has_effect",
                polarity="promotes",
                selector={**rsel, "columns": ["Event ID", "ISO3", "Year", "Disaster Internal Displacements (Raw)",
                                              "Displacement occurred"]},
                raw_sha256=sha,
                codebook_term="disaster_displacement",
                source_claim_id=f"{key}:{iso}:{year}",
                scope={k: v for k, v in {"place_iso3": iso, "year": int(year),
                                         "displacement_occurred": occurred}.items() if v is not None},
            ))
    return {"entities": list(ents.values()), "statements": sts, "claims": claims, "anomalies": dict(anomalies),
            "events": len(by_event)}


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Internal Displacement Monitoring Centre (IDMC), via the Humanitarian Data Exchange (HDX)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HDX_HOST, S3_HOST),
        datasets=(DATASET,),
        dataset_ids=(f"hdx:{PACKAGE}", RESOURCE_NAME),
        cadence_seconds=90 * 86400,
        planned_requests=3,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "kg": "idmc:event:<Event ID | r<row sha1>>, idmc:displacement:<event>:<ISO3>:<Year>",
            "claims": "registry/causal/relations_causmore.yaml dataset idmc_disasters (has_effect)",
        },
        notes="KG-only source (no atlas table). Codebook = the workbook's README sheet (verbatim check).",
    )


def build(raw: Any, *, country_qids: Mapping[str, str], strict: bool = True) -> dict[str, Any]:
    sha = hashlib.sha256(raw.body).hexdigest()
    rows, texts = parse(raw.body, strict=strict)
    out = build_kg(rows, sha=sha, country_qids=country_qids)
    cm.check_unique(out["entities"], out["statements"])
    return {**out, "sha": sha, "rows": len(rows), "readme": cm.check_texts(CLAIMS_DATASET, texts)}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """HDX package record -> disaster workbook; archive both; load the KG records; deliver the claims."""
    if not run.wants(DATASET):
        return {"skipped": True}
    pkg = await run.get(PACKAGE_URL, key="hdx:package_show", media_types=("application/json",), max_bytes=2_000_000)
    url, meta = resource_url(pkg.body)
    raw = await run.get(url, key="idmc:gidd_disasters", media_types=XLSX_MEDIA, max_bytes=MAX_BYTES)
    qids = cm.country_qids()
    out = build(raw, country_qids=qids, strict=run.origin == "live")
    run.archive(pkg)
    run.archive(raw)
    res: dict[str, Any] = {
        "hdx": meta,
        "rows": out["rows"],
        "events": out["events"],
        "anomalies": {k: (v if len(v) <= 30 else [*v[:30], f"... {len(v)} in total"]) for k, v in
                      out["anomalies"].items()},
        "codebook": {k: out["readme"][k] for k in ("sha256", "matches", "missing_definitions")},
        "kg_counts": {"entities": len(out["entities"]), "statements": len(out["statements"])},
    }
    if cd.kg_available(run.engine):
        res["kg"] = cm.load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision=f"gidd-disasters:{meta.get('resource_last_modified') or out['sha'][:12]}"[:100],
            raw_sha256s=[out["sha"]],
            retrieved_at=raw.retrieved_at,
            origin=run.origin,
        )
    if not out["readme"]["matches"]:
        res["claims_withheld"] = {CLAIMS_DATASET: "README definitions differ from the quoted ones"}
        res["claims"] = {}
        return res
    res["claims"] = cd.deliver_claims(run, [cm.delivery(
        CLAIMS_DATASET, claims=out["claims"], raw_sha256s=[out["sha"]], retrieved_at=raw.retrieved_at,
        origin=run.origin, documents_sha256={"idmc_readme": out["readme"]["sha256"]})])
    return res
