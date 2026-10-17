"""Small shared shapes of the Fiche bundle: source chips, licences, references, KG literal values and periods."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from reva.v8.entity import profiles as P

SOURCE_NAMES = {
    "wikidata": "Wikidata",
    "chebi.release": "ChEBI",
    "hgnc.complete_set": "HGNC",
    "mondo.obo": "Mondo",
    "chembl.api": "ChEMBL",
    "unichem.mapping": "UniChem",
    "who.eeml": "OMS — liste des médicaments essentiels",
    "gleif.api": "GLEIF",
    "wri.gppd": "GPPD (WRI)",
    "ucdp.downloads": "UCDP",
    "gbif.backbone": "GBIF",
    "usgs.sciencebase": "USGS",
}


def source_name(key: str | None) -> str:
    if not key:
        return "source inconnue"
    if key in SOURCE_NAMES:
        return SOURCE_NAMES[key]
    head = key.split(".", 1)[0]
    return SOURCE_NAMES.get(head, head.upper() if len(head) <= 5 else head.replace("_", " ").capitalize())


def licence_of(rights: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not rights:
        return None
    lic = rights.get("licence")
    return {
        "licence": str(lic) if lic is not None else None,
        "licence_url": rights.get("licence_url"),
        "attribution": rights.get("attribution"),
        "restricted": bool(rights.get("restricted")),
        "non_commercial": bool(rights.get("non_commercial")),
        "share_alike": bool(rights.get("share_alike")),
        "required_notice": rights.get("required_notice"),
    }


def iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def ref(kind: str | None, ident: str, label: str | None, *, loaded: bool = True) -> dict[str, Any]:
    """A link target in the bundle; the UI builds the URL with ``v8Links.entity(kind, id)``."""
    return {"kind": kind or "entity", "id": ident, "label": label or ident, "loaded": loaded}


def kg_ref(r: Mapping[str, Any], *, loaded: bool = True) -> dict[str, Any]:
    qid = str(r.get("qid") or r.get("id"))
    label = r.get("label")
    if not label or label == qid:
        label = qid
    return ref(r.get("kind"), qid, str(label), loaded=bool(r.get("entity_loaded", loaded)))


# ------------------------------------------------------------------------------------------- KG literal values
def time_text(t: Mapping[str, Any] | None) -> str | None:
    """Display of a Wikibase-style time ({time, precision}): year, year-month or full date (astronomical years)."""
    if not t or not t.get("time"):
        return None
    s = str(t["time"]).lstrip("+")
    prec = int(t.get("precision") or 11)
    m = re.match(r"^(-?\d{1,7})(?:-(\d{2}))?(?:-(\d{2}))?", s)
    if not m:
        return s
    y, mo, d = m.group(1), m.group(2), m.group(3)
    y = str(int(y))
    if prec <= 9 or not mo:
        return y
    if prec == 10 or not d:
        return f"{y}-{mo}"
    return f"{y}-{mo}-{d}"


def time_x(t: Mapping[str, Any] | None) -> float | None:
    """Numeric position (decimal year) of a time, for sparklines."""
    s = time_text(t)
    if s is None:
        return None
    m = re.match(r"^(-?\d+)(?:-(\d{2}))?(?:-(\d{2}))?$", s)
    if not m:
        return None
    y = int(m.group(1))
    mo = int(m.group(2)) if m.group(2) else 0
    d = int(m.group(3)) if m.group(3) else 0
    return y + (max(mo - 1, 0) / 12 if mo else 0) + (d / 365 if d else 0)


def statement_period(st: Mapping[str, Any]) -> str | None:
    """Period of a KG statement from its validity qualifiers (point in time, else start–end)."""
    pit = time_text(st.get("point_in_time"))
    if pit:
        return pit
    a, b = time_text(st.get("start")), time_text(st.get("end"))
    if a and b:
        return a if a == b else f"{a} – {b}"
    if a:
        return f"depuis {a}"
    if b:
        return f"jusqu’à {b}"
    return None


def statement_x(st: Mapping[str, Any]) -> float | None:
    return time_x(st.get("point_in_time")) or time_x(st.get("start")) or time_x(st.get("end"))


def literal_value(pid: str, v: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{value (decimal string) | None, text, unit, lo, hi, url}`` of a literal statement value; never invents a
    number: a quantity keeps its decimal string, anything else is text."""
    out: dict[str, Any] = {"value": None, "text": "", "unit": None, "lo": None, "hi": None, "url": None}
    if not isinstance(v, Mapping):
        out["text"] = "" if v is None else str(v)
        return out
    typ = v.get("type")
    if typ == "quantity" or ("amount" in v and typ in (None, "quantity")):
        amt = str(v.get("amount", "")).lstrip("+")
        out["value"] = amt or None
        out["text"] = amt
        unit = v.get("unit")
        out["unit"] = None if unit in (None, "1", "") else str(unit)
        if v.get("lowerBound") is not None:
            out["lo"] = str(v["lowerBound"]).lstrip("+")
        if v.get("upperBound") is not None:
            out["hi"] = str(v["upperBound"]).lstrip("+")
        return out
    if typ == "time" or ("time" in v and "precision" in v):
        out["text"] = time_text(v) or str(v.get("time"))
        return out
    if typ == "globe_coordinate" or ("lat" in v and "lon" in v):
        out["text"] = f"{v.get('lat')}, {v.get('lon')}"
        return out
    if typ == "monolingual_text" or "text" in v:
        out["text"] = str(v.get("text") or "")
        return out
    val = v.get("value")
    out["text"] = "" if val is None else str(val)
    if typ == "external_id" and out["text"]:
        out["url"] = P.value_url(pid, out["text"])
    return out


def kg_source(
    st: Mapping[str, Any], rights: Mapping[str, Any] | None = None, period: str | None = None
) -> dict[str, Any]:
    """Source chip of a KG statement (its own provenance and the licence of the raw artifact it came from)."""
    prov = st.get("provenance") or {}
    key = str(prov.get("source_key") or "")
    prop = st.get("property") or {}
    return {
        "source": key,
        "label": source_name(key),
        "dataset": None,
        "statement_key": prov.get("statement_key"),
        "series": None,
        "property": prop.get("pid"),
        "raw_sha256": prov.get("raw_sha256"),
        "selector": None,
        "retrieved_at": iso(prov.get("retrieved_at")),
        "period": period,
        "licence": licence_of(rights),
        "verification": prov.get("verification"),
        "origin": prov.get("origin"),
        "href": None,
    }


def atlas_source(cell: Mapping[str, Any], *, out_of_snapshot: bool = False) -> dict[str, Any]:
    """Source chip of an atlas cell (table, series, raw artifact + selector, licence of the table artifact)."""
    prov = cell.get("provenance") or {}
    src = str(cell.get("source") or "")
    ps, pe = cell.get("period_start"), cell.get("period_end")
    return {
        "source": f"{src}.{cell.get('dataset')}" if cell.get("dataset") else src,
        "label": source_name(src),
        "dataset": prov.get("dataset_key"),
        "statement_key": None,
        "series": cell.get("series"),
        "property": None,
        "raw_sha256": prov.get("raw_sha256"),
        "selector": prov.get("selector") if isinstance(prov.get("selector"), dict) else None,
        "retrieved_at": None,
        "period": f"{str(ps)[:10]} → {str(pe)[:10]}" if ps and pe else None,
        "licence": licence_of(cell.get("rights")),
        "verification": "outside_atlas_snapshot" if out_of_snapshot else "source_reported_not_verified",
        "origin": None,
        "href": None,
    }


def period_label(start: str | None, end: str | None, basis: str | None = None) -> str | None:
    """Readable period of an atlas cell: "2024" (calendar year), "2024-03" (month), else start → end."""
    if not start:
        return None
    s, e = str(start)[:10], str(end or "")[:10]
    if basis == "calendar_year" or (s.endswith("-01-01") and e.endswith("-01-01") and _years_between(s, e) == 1):
        return s[:4]
    if s.endswith("-01") and e.endswith("-01") and _months_between(s, e) == 1:
        return s[:7]
    if s.endswith("-01") and e.endswith("-01") and _months_between(s, e) == 3:
        q = (int(s[5:7]) - 1) // 3 + 1
        return f"{s[:4]}-T{q}"
    if s == e or not e or _days_between(s, e) == 1:
        return s
    return f"{s} → {e}"


def _days_between(a: str, b: str) -> int:
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except ValueError:
        return -1


def _years_between(a: str, b: str) -> int:
    try:
        return int(b[:4]) - int(a[:4])
    except ValueError:
        return -1


def _months_between(a: str, b: str) -> int:
    try:
        return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])
    except ValueError:
        return -1


def period_x(start: str | None) -> float | None:
    if not start:
        return None
    m = re.match(r"^(-?\d{1,6})-(\d{2})-(\d{2})", str(start))
    if not m:
        return None
    return int(m.group(1)) + (int(m.group(2)) - 1) / 12 + (int(m.group(3)) - 1) / 365
