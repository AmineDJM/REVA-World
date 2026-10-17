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
  ``idmc:event:r<sha256[:16] of the row's ISO3|Year|Event Name|Date|Hazard Sub Type|raw figure|event codes>`` -- rows are
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

FIXB (1.1.0, second causal audit ``docs/dev/review_v3_causal_audit2.md``)
-------------------------------------------------------------------------
* N4 -- a row flagged "Displacement reporting preventive evacuations" or whose IDMC-coded name describes a hazard
  *risk* / threat / false alarm / pre-emptive move gives NO causal claim: its displacement is (also) "in anticipation
  of" a hazard that may never have struck (README A24, A52). It is the context statement
  ``idmc:displacement_in_anticipation_of`` (displacement -> event) with the basis, the verbatim flag and both README
  passages; the figure stays on the displacement record, whose label says so.
* N5 -- the README does not say whether a 1 January start is a real day or a year-level placeholder, so a start coded
  as 1 January is stored at year precision with the coded value kept verbatim (``coded_start``) and the reason
  (``day_not_relied_on``): REVA never relies on a day the source may not code.
* N8 -- the claims' ``codebook.quote_source`` is the README cell of the archived workbook (its sha256).
* N12 -- an event REVA keyed from a row without an Event ID is labelled "IDMC record without an Event ID - identity
  created by REVA" / "enregistrement IDMC sans identifiant d'événement — identité créée par REVA"; fully identical rows
  loaded once leave the provenance note ``idmc:source_duplicate_rows`` (count + row selectors) on the kept record.
* The KG source revision names the connector version (``…+reva-<version>``): changed content is a new revision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors import causmore_common as cm
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement

NAME = "atlas.idmc"
VERSION = "1.1.0"  # 1.1.0 (FIXB, audit2 N4/N5/N8/N12): anticipatory displacement is context, 1 January starts at
#: year precision, codebook quote_source (README cells of the archived workbook), REVA-made identities labelled
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
    basis = "|".join(
        r[c]
        for c in (
            "ISO3",
            "Year",
            "Event Name",
            "Date of Event (start)",
            "Hazard Sub Type",
            "Disaster Internal Displacements (Raw)",
            "Event Codes (Code:Type)",
        )
    )
    return "r" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


#: "Displacement occurred" value of rows that report preventive evacuations (README A52).
PREVENTIVE = "Displacement reporting preventive evacuations"
NOT_PREVENTIVE = "Displacement without preventive evacuations reported"
#: Words of an IDMC-coded event name that describe a hazard risk, a threat, a false alarm or a pre-emptive move, not a
#: hazard that struck (audit2 N4). README A43: names are coded from "the country, type of hazard, location, and event
#: start date" when no common name exists, so such a word is IDMC's own description of the event.
ANTICIPATORY_NAME = re.compile(
    r"\b(risks?|threats? of|false alarm|pre-?emptive|prevent(?:ive|ative)|precautionary)\b", re.IGNORECASE
)
README_A24 = (
    "Disaster displacement: Refers to situations where people are forced to leave their homes or places of habitual "
    "residence as a result, or in anticipation of the negative impact of natural hazards."
)
README_A52 = (
    "Displacement Occurred: This field contains values that represent if preventive evacuations were reported. These "
    "evacuations are the result of existing early warning systems."
)
#: an IDMC-coded name ending in the date "01/01/<year>" (README A43: coded "based on ... event start date")
PLACEHOLDER_NAME = re.compile(r"-\s*01/01/\d{4}\s*$")
REVA_IDENTITY_EN = "IDMC record without an Event ID - identity created by REVA"
REVA_IDENTITY_FR = "enregistrement IDMC sans identifiant d'événement — identité créée par REVA"
JAN1_NOTE = (
    "IDMC codes this event's start as 1 January. Its README calls the start 'Approximate start date of the event' "
    "(A44) and names events without a common name after 'the event start date' (A43), but does not say whether a "
    "1 January start is a real day or a year-level placeholder; REVA keeps the coded value (coded_start) and relies "
    "on its year only (precision year), never on the day."
)


def anticipatory_basis(r: Mapping[str, str]) -> list[str]:
    """Why a row's displacement is (also) in anticipation of the hazard rather than an effect of a hazard that struck:
    IDMC's own flag (preventive evacuations reported) and/or IDMC's event name (risk, threat, false alarm, pre-emptive
    / preventive). Empty for an ordinary row."""
    out = []
    if r["Displacement occurred"] == PREVENTIVE:
        out.append("displacement_occurred")
    m = ANTICIPATORY_NAME.search(r["Event Name"] or "")
    if m:
        out.append(f"event_name:{m.group(1).lower()}")
    return out


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
        kind_text = f"Disaster event ({sub or first['Hazard Type'] or 'hazard not given'}) in the IDMC Global Internal "
        kind_text += "Displacement Database"
        if key.startswith("r"):
            # audit2 N12: the identity is REVA's (sha256 of the row's fields), never grouped with same-name rows
            ents[eid] = KgEntity(
                qid=eid,
                kind="disaster_event",
                labels={"en": f"{name} ({REVA_IDENTITY_EN})", "fr": f"{name} ({REVA_IDENTITY_FR})"},
                descriptions={
                    "en": f"{kind_text}. {REVA_IDENTITY_EN}: the key r{key[1:]} is the sha256 of the row's fields "
                    "(ISO3, Year, Event Name, Date, Hazard Sub Type, raw figure, event codes); not linked to other "
                    "rows of the same name",
                    "fr": f"Événement de catastrophe ({sub or first['Hazard Type'] or 'aléa non précisé'}) de la base "
                    f"GIDD de l'IDMC. {REVA_IDENTITY_FR} : la clé r{key[1:]} est le sha256 des champs de la ligne ; "
                    "non reliée aux autres lignes de même nom",
                },
            )
        else:
            ents[eid] = KgEntity(
                qid=eid,
                kind="disaster_event",
                labels={"en": name},
                descriptions={"en": f"{kind_text}, IDMC Event ID {key}"},
            )
        if len({(r["Hazard Category"], r["Hazard Type"], r["Hazard Sub Type"]) for r in erows}) > 1:
            anomalies["event_with_several_hazards"].append(key)
        if sub:
            sts.append(
                cm.string_stmt(
                    f"e{key}:hazard",
                    eid,
                    "idmc:hazard_sub_type",
                    sub,
                    raw_sha256=sha,
                    selector={**sel, "columns": ["Hazard Category", "Hazard Type", "Hazard Sub Type"]},
                    hazard_category=first["Hazard Category"] or None,
                    hazard_type=first["Hazard Type"] or None,
                )
            )
        dates = sorted({d for d in (excel_date(r["Date of Event (start)"]) for r in erows) if d is not None})
        if len(dates) > 1:
            anomalies["event_with_several_start_dates"].append(key)
        if dates:
            tq: dict[str, Any] = {
                "definition": "Date of Event (Start): Approximate start date of the event.",
                "approximate": True,
            }
            start = (dates[0], 11)
            if dates[0][5:] == "01-01":  # audit2 N5: a 1 January start is used at year precision, never as a day
                start = (dates[0], 9)
                placeholder_name = any(
                    PLACEHOLDER_NAME.search(r["Event Name"] or "")
                    and excel_date(r["Date of Event (start)"]) == dates[0]
                    for r in erows
                )
                tq.update(
                    coded_start=dates[0],
                    day_not_relied_on={
                        "reason": "start_coded_as_1_january",
                        "name_carries_01_01": bool(placeholder_name),
                        "note": JAN1_NOTE,
                    },
                )
                anomalies["start_1_january_year_precision"].append(key)
            sts.extend(
                cd.event_times(
                    eid,
                    f"e{key}:t",
                    start=start,
                    raw_sha256=sha,
                    selector={**sel, "column": "Date of Event (start)"},
                    qualifiers=tq,
                )
            )
        for iso in sorted({r["ISO3"] for r in erows if r["ISO3"]}):
            q = country_qids.get(iso)
            if q:
                sts.append(
                    cm.item_stmt(
                        f"e{key}:country:{iso}",
                        eid,
                        "P17",
                        q,
                        raw_sha256=sha,
                        selector={**sel, "column": "ISO3"},
                        iso3=iso,
                    )
                )
            else:
                sts.append(
                    cm.string_stmt(
                        f"e{key}:country:{iso}",
                        eid,
                        "idmc:country",
                        iso,
                        raw_sha256=sha,
                        selector={**sel, "column": "ISO3"},
                        note="ISO3 not in the crosswalk",
                    )
                )
        codes = sorted({c for r in erows for c in event_codes(r["Event Codes (Code:Type)"])})
        for j, (code, typ) in enumerate(codes):
            pid = "idmc:glide_number" if typ.lower() == "glide number" else "idmc:event_code"
            sts.append(
                cm.string_stmt(
                    f"e{key}:code:{j}",
                    eid,
                    pid,
                    code,
                    raw_sha256=sha,
                    selector={**sel, "column": "Event Codes (Code:Type)"},
                    code_type=typ or None,
                )
            )
        # fully identical provider rows (only rows without an Event ID in the 2025-05-13 file): loaded once, and the
        # collapse recorded in the KG as a provenance note on the kept record (audit2 N12)
        same: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for r in erows:
            same[tuple(f"{k}={v}" for k, v in sorted(r.items()) if k != "_row")].append(int(r["_row"]))
        copies = {min(v): sorted(v) for v in same.values() if len(v) > 1}
        for r in erows:
            iso, year = r["ISO3"], r["Year"]
            if not iso or not year.isdigit():
                anomalies["row_without_country_or_year"].append(r["_row"])
                continue
            did = f"idmc:displacement:{key}:{iso}:{year}"
            rsel = {
                "file": RESOURCE_NAME,
                "sheet": DATA_SHEET,
                "row": int(r["_row"]),
                "event_key": key,
                "ISO3": iso,
                "Year": int(year),
            }
            if did in ents:
                if not any(int(r["_row"]) in v for v in copies.values()):
                    anomalies["duplicate_event_country_year"].append(did)  # a different row: reported, never merged
                continue
            raw_n = _amount(r["Disaster Internal Displacements (Raw)"])
            basis = anticipatory_basis(r)
            label = f"Internal displacements in {r['Country / Territory'] or iso} ({year}): {name}"
            desc = f"Disaster internal displacements reported by IDMC for {iso} in {year}" + (
                f" ({raw_n})" if raw_n else ""
            )
            if basis:
                label += " [in anticipation of the hazard: preventive evacuation or hazard risk, per IDMC]"
                desc += (
                    "; IDMC reports this displacement in anticipation of the hazard (preventive evacuations or a "
                    "hazard risk), so REVA records no causal claim from the event"
                )
            ents[did] = KgEntity(qid=did, kind="disaster_displacement", labels={"en": label}, descriptions={"en": desc})
            dup_rows = copies.get(int(r["_row"]))
            if dup_rows:
                sts.append(
                    cm.string_stmt(
                        f"d{key}:{iso}:{year}:source_duplicates",
                        did,
                        "idmc:source_duplicate_rows",
                        f"{len(dup_rows)} fully identical IDMC rows (rows {', '.join(map(str, dup_rows))} of sheet "
                        f"{DATA_SHEET}) loaded once",
                        raw_sha256=sha,
                        selector={"file": RESOURCE_NAME, "sheet": DATA_SHEET, "rows": dup_rows, "event_key": key},
                        count=len(dup_rows),
                        rows=dup_rows,
                        kept_row=dup_rows[0],
                        note="every column equal, no Event ID; no figure is lost (the copies repeat the same figure)",
                    )
                )
                anomalies["identical_rows_loaded_once"].append(",".join(map(str, dup_rows)))
            sts.append(
                cm.string_stmt(
                    f"d{key}:{iso}:{year}:type",
                    did,
                    "causmore:event_type",
                    "disaster_displacement",
                    raw_sha256=sha,
                    selector=rsel,
                )
            )
            sts.extend(
                cd.event_times(
                    did,
                    f"d{key}:{iso}:{year}:t",
                    point=(f"{int(year):04d}-01-01", 9),
                    raw_sha256=sha,
                    selector={**rsel, "column": "Year"},
                    qualifiers={"definition": "Year: Indicates the year for which displacement data are reported."},
                )
            )
            q = country_qids.get(iso)
            if q:
                sts.append(
                    cm.item_stmt(
                        f"d{key}:{iso}:{year}:country",
                        did,
                        "P17",
                        q,
                        raw_sha256=sha,
                        selector={**rsel, "column": "ISO3"},
                        iso3=iso,
                    )
                )
            else:
                sts.append(
                    cm.string_stmt(
                        f"d{key}:{iso}:{year}:country",
                        did,
                        "idmc:country",
                        iso,
                        raw_sha256=sha,
                        selector={**rsel, "column": "ISO3"},
                    )
                )
            occurred = r["Displacement occurred"] or None
            if raw_n is not None:
                sts.append(
                    KgStatement(
                        key=f"d{key}:{iso}:{year}:n",
                        subject=did,
                        property="idmc:internal_displacements",
                        value={"type": "quantity", "amount": raw_n, "unit": "internal displacements"},
                        qualifiers={
                            k: v
                            for k, v in {
                                "column": "Disaster Internal Displacements (Raw)",
                                "rounded": _amount(r["Disaster Internal Displacements"]),
                                "displacement_occurred": occurred,
                            }.items()
                            if v is not None
                        },
                        raw_sha256=sha,
                        selector={**rsel, "column": "Disaster Internal Displacements (Raw)"},
                    )
                )
            if raw_n is None or float(raw_n) <= 0:
                anomalies["no_positive_figure"].append(r["_row"])
                continue
            if basis:
                # audit2 N4: displacement "in anticipation of" a hazard (README A24) is not an effect of a hazard
                # event that may never have struck -> a context statement, never a causal claim
                disagree = occurred == NOT_PREVENTIVE and "displacement_occurred" not in basis
                sts.append(
                    KgStatement(
                        key=f"d{key}:{iso}:{year}:anticipation",
                        subject=did,
                        property="idmc:displacement_in_anticipation_of",
                        object_qid=eid,
                        qualifiers={
                            k: v
                            for k, v in {
                                "basis": basis,
                                "displacement_occurred": occurred,
                                "event_name": r["Event Name"] or None,
                                "source_flags_disagree": True if disagree else None,
                                "definition": {"README!A24": README_A24, "README!A52": README_A52},
                                "note": "not a causal claim: IDMC counts displacement 'as a result, or in "
                                "anticipation of' a hazard; for this row the figure includes displacement in "
                                "anticipation (preventive evacuation or a hazard risk), which REVA does not word "
                                "as an effect of the event",
                            }.items()
                            if v is not None
                        },
                        raw_sha256=sha,
                        selector={**rsel, "columns": ["Event Name", "Displacement occurred"]},
                    )
                )
                anomalies["anticipatory_no_claim"].append(r["_row"])
                if disagree:
                    anomalies["anticipatory_name_but_flag_without_preventive"].append(r["_row"])
                continue
            claims.append(
                cd.ClaimRecord(
                    cause_id=eid,
                    effect_id=did,
                    relation="has_effect",
                    polarity="promotes",
                    selector={
                        **rsel,
                        "columns": [
                            "Event ID",
                            "ISO3",
                            "Year",
                            "Disaster Internal Displacements (Raw)",
                            "Displacement occurred",
                        ],
                    },
                    raw_sha256=sha,
                    codebook_term="disaster_displacement",
                    source_claim_id=f"{key}:{iso}:{year}",
                    scope={
                        k: v
                        for k, v in {"place_iso3": iso, "year": int(year), "displacement_occurred": occurred}.items()
                        if v is not None
                    },
                )
            )
    return {
        "entities": list(ents.values()),
        "statements": sts,
        "claims": claims,
        "anomalies": dict(anomalies),
        "events": len(by_event),
    }


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
            "kg": "idmc:event:<Event ID | r<row sha256[:16]>>, idmc:displacement:<event>:<ISO3>:<Year>",
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
        "anomalies": {
            k: (v if len(v) <= 30 else [*v[:30], f"... {len(v)} in total"]) for k, v in out["anomalies"].items()
        },
        "codebook": {
            **{k: out["readme"][k] for k in ("matches", "missing_definitions")},
            "readme_text_sha256": out["readme"]["sha256"],
            "workbook_sha256": out["sha"],
        },
        "anticipatory_rows_no_claim": len(out["anomalies"].get("anticipatory_no_claim", [])),
        "kg_counts": {"entities": len(out["entities"]), "statements": len(out["statements"])},
    }
    if cd.kg_available(run.engine):
        res["kg"] = cm.load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision=f"gidd-disasters:{meta.get('resource_last_modified') or out['sha'][:12]}"[:86]
            + f"+reva-{VERSION}",
            raw_sha256s=[out["sha"]],
            retrieved_at=raw.retrieved_at,
            origin=run.origin,
        )
    if not out["readme"]["matches"]:
        res["claims_withheld"] = {CLAIMS_DATASET: "README definitions differ from the quoted ones"}
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
                # the README is a sheet of the archived workbook: its quotes cite that FILE (sha256) and cell (N8)
                documents_sha256={"idmc_readme": out["sha"]},
                archived={"idmc_readme": out["sha"]},
            )
        ],
    )
    return res
