"""COW War Data v4 (inter-, extra-, intra- and non-state wars): war events, coded outcomes (T1 claims) and war
transformations (context) (agent CAUSMORE).

Source and access
-----------------
Correlates of War Project, ``correlatesofwar.org/data-sets/cow-war/`` (no account), registered source ``cow.data``
(shared with ``atlas.cow`` and ``atlas.mid``), one request per file:

* ``Inter-StateWarData_v4.0.csv``, ``Extra-StateWarData_v4.0.csv``, ``Intra-StateWarData_v4.1.csv``,
  ``Non-StateWarData_v4.0.csv`` (Sarkees and Wayman 2010, *Resort to War*), CR line endings, Windows-1252 text;
* their four codebook PDFs, whose sha256 must equal ``registry/causal/relations_causmore.yaml`` before claims are
  delivered (a changed codebook withholds that file's claims; events and context still load).

**Terms** (COW FAQ, as ``atlas.cow``): cite the data, users cannot charge for access -- not an open licence.

Why intra-state v4.1 and not v5.1: the inter-/extra-/non-state v4.0 files code transformations with v4 war numbers,
``atlas.cow`` keys its ``cow:war:<n>`` entities by v4 numbers and PA-X's ``CowWar`` cites them; intra-state v5.1
renumbers wars and reuses numbers for other wars (856 = "Eighth Colombia" in v4.1, "Romanian War of 1989" in v5.1).
One id namespace must mean one numbering, so v5.1 is left for a separate namespace (follow-up). From 1.1.0 (FIXB,
audit2 N8) the provider's ``Intra-State-Wars-v5.1.zip`` is fetched and archived as the evidence of that choice and
:func:`v51_numbering_check` recomputes it from the archived bytes (reported, never loaded); the claims carry
``codebook.quote_source`` (archived codebook PDF sha256 + page).

Outputs (semantics and reasons: ``registry/causal/relations_causmore.yaml`` datasets ``cow_inter`` ... )
-------
* KG (source key ``cow.data``): ``cow:war:<n>`` entities for intra- and non-state wars (inter-/extra-state war
  entities are ``atlas.cow``'s and are not re-emitted); for every war ``cowwar:war_type``, ``cowwar:outcome``,
  ``P580``/``P582`` (earliest coded start / latest coded end over the war's records -- a REVA summary of the coded
  spells, said so in the qualifiers; no end when a spell is coded ongoing, -7), side names, initiator (a participant
  role, never a cause), ``P710`` state participants of intra-state wars (``cow:<ccode>``).
* ``cowwar:outcome:<n>[:<ccode>:<k>]`` outcome records (inter-state: one per state participant record) and the T1
  claims ``coded_outcome`` war -> outcome (codebook term ``Outcome``); code 5 (ongoing) gives no outcome record.
* ``cowwar:transformed_into`` war -> war, from each file's ``TransTo`` (stated on the row of the earlier war) and
  ``TransFrom`` (stated on the row of the later war): context only. The codebook: "the initial war ends and the
  conflict is transformed into a war of a second type" -- one conflict changing category, not a cause.
* No atlas table: the participation and battle-death tables are ``atlas.cow``'s (``cow.wars4``); the declared
  dataset ``wars_kg`` only gates the KG load.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors import causmore_common as cm
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement

NAME = "atlas.cowwars"
VERSION = "1.1.0"  # 1.1.0 (FIXB, audit2 N8): codebook quote_source; COW intra-state v5.1 archived as numbering evidence
PARSER_VERSION = "atlas_cowwars/1"
SOURCE_KEY = "cow.data"
ATLAS_SOURCE = "cow"
DATASET = "wars_kg"
HOST = "correlatesofwar.org"
BASE = f"https://{HOST}/wp-content/uploads/"
KINDS = ("inter", "extra", "intra", "nonstate")
FILES = {
    "inter": "Inter-StateWarData_v4.0.csv",
    "extra": "Extra-StateWarData_v4.0.csv",
    "intra": "Intra-StateWarData_v4.1.csv",
    "nonstate": "Non-StateWarData_v4.0.csv",
}
DATASETS = {k: f"cow_{k}" for k in KINDS}
CODEBOOKS = {k: f"cow_{k}_codebook" for k in KINDS}
VERSIONS = {"inter": "4.0", "extra": "4.0", "intra": "4.1", "nonstate": "4.0"}
MIN_ROWS = {"inter": 300, "extra": 150, "intra": 400, "nonstate": 50}
MEDIA = ("text/csv", "application/csv", "application/octet-stream", "text/plain", "application/vnd.ms-excel")
PDF_MEDIA = ("application/pdf", "application/octet-stream")
MAX_BYTES = 4_000_000
WAR_KIND_LABEL = {
    "inter": "Inter-state war",
    "extra": "Extra-state war",
    "intra": "Intra-state war",
    "nonstate": "Non-state war",
}
#: Outcome codes that are an outcome (5 = still ongoing at the end of the data is none).
OUTCOME_CLAIM_CODES = frozenset({1, 2, 3, 4, 6, 7, 8})
MISSING = ("", "-9", "-8", "-7")

RIGHTS: dict[str, Any] = {
    "licence": "COW terms of use (not an open licence): cite the data; users cannot charge for access",
    "terms_url": "https://correlatesofwar.org/faq/",
    "open_licence": False,
    "attribution": "Sarkees, Meredith Reid and Frank Wayman (2010), Resort to War: 1816-2007 (COW War Data v4.0, "
    "Intra-State War Data v4.1). Online, https://correlatesofwar.org",
}

_COMMON = ("WarNum", "WarName", "WarType", "Outcome", "TransFrom", "TransTo", "Initiator", "Version")
REQUIRED = {
    "inter": (*_COMMON, "ccode", "StateName", "Side", "StartYear1", "EndYear1", "StartYear2", "EndYear2"),
    "extra": (*_COMMON, "ccode1", "SideA", "ccode2", "SideB", "StartYear1", "EndYear1", "StartYear2", "EndYear2"),
    "intra": (*_COMMON, "CcodeA", "SideA", "CcodeB", "SideB", "StartYear1", "EndYear1", "StartYear2", "EndYear2"),
    "nonstate": (*_COMMON, "SideA1", "SideB1", "StartYear", "EndYear"),
}


#: COW Intra-State War Data v5.1 (Dixon & Sarkees 2016), the provider's zip (data CSV + codebook with its "Changes
#: since Guide" list): fetched and archived only as the evidence of the v4.1 numbering choice (audit2 N8), never
#: loaded as wars (it renumbers and reuses war numbers).
V51_URL = BASE + "Intra-State-Wars-v5.1.zip"
V51_MEMBER = "INTRA-STATE WARS v5.1 CSV.csv"
V51_CODEBOOK_MEMBER = "Codebook for Intra-state v5.1 2.9.20.pdf"
ZIP_MEDIA = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
#: REVA's normalisation to compare v4.1 and v5.1 war names: v5.1 appends " [War] of <years>" (e.g. "Argentine Indians
#: War of 1878-1879" vs v4.1 "Argentine Indians"); a heuristic for the report only, never used to link records.
_V51_SUFFIX = re.compile(r"\s+(?:war\s+)?of\s+\d{4}.*$", re.IGNORECASE)


def _norm_name(name: str) -> str:
    return " ".join(_V51_SUFFIX.sub("", name or "").lower().split())


def v51_numbering_check(v41_rows: Sequence[Mapping[str, str]], v51_zip: bytes) -> dict[str, Any]:
    """Offline check, from the archived bytes, of why intra-state v5.1 is not mixed into the v4 ``cow:war:<n>``
    namespace: war numbers of v4.1 absent from v5.1, shared numbers whose v5.1 war starts in another year or has
    another (normalised) name, and the example 856."""
    try:
        z = zipfile.ZipFile(io.BytesIO(v51_zip))
        body = z.read(V51_MEMBER)
        members = sorted(z.namelist())
    except (zipfile.BadZipFile, KeyError) as e:
        raise cd.drift(f"COW intra-state v5.1 zip: unreadable or no {V51_MEMBER}") from e
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("cp1252")
    v51: dict[str, tuple[str, int | None]] = {}
    for r in csv.DictReader(io.StringIO(text.replace("\r\n", "\n").replace("\r", "\n"))):
        n = war_number(r.get("WarNum"))
        if n is not None:
            v51.setdefault(n, ((r.get("WarName") or "").strip(), _int(r.get("StartYr1"))))
    v41: dict[str, tuple[str, int | None]] = {}
    for row in v41_rows:
        n = war_number(row["WarNum"])
        if n is not None:
            v41.setdefault(n, (row["WarName"].strip(), _int(row.get("StartYear1"))))
    shared = sorted(set(v41) & set(v51), key=float)
    other_year = [n for n in shared if v41[n][1] != v51[n][1]]
    other_name = [n for n in shared if _norm_name(v41[n][0]) != _norm_name(v51[n][0])]
    return {
        "v51_members": members,
        "v41_wars": len(v41),
        "v51_wars": len(v51),
        "shared_numbers": len(shared),
        "v41_numbers_absent_in_v51": sorted(set(v41) - set(v51), key=float),
        "v51_numbers_not_in_v41": len(set(v51) - set(v41)),
        "shared_numbers_other_start_year": [
            {"WarNum": n, "v4.1": list(v41[n]), "v5.1": list(v51[n])} for n in other_year
        ],
        "shared_numbers_other_name": [{"WarNum": n, "v4.1": v41[n][0], "v5.1": v51[n][0]} for n in other_name],
        "example_856": {"v4.1": v41.get("856", ("",))[0] or None, "v5.1": v51.get("856", ("",))[0] or None},
        "name_comparison": "REVA normalisation (v5.1 ' [War] of <years>' suffix dropped, case folded): a report "
        "heuristic, never used to link records",
    }


def file_url(kind: str) -> str:
    return BASE + FILES[kind]


def codebook_url(kind: str) -> str:
    return str(cm.document(CODEBOOKS[kind])["download_url"])


# ----------------------------------------------------------------------------- parsing


def parse(kind: str, body: bytes, *, strict: bool = True) -> list[dict[str, str]]:
    """Rows of one COW war file (UTF-8 if valid, else Windows-1252 -- the provider's encoding; CR line endings)."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("cp1252", errors="strict")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    reader = csv.DictReader(io.StringIO(text))
    cols = [(c or "").strip() for c in reader.fieldnames or []]
    missing = [c for c in REQUIRED[kind] if c not in cols]
    if missing:
        raise cd.drift(f"{FILES[kind]}: missing columns {missing}")
    rows = []
    for i, r in enumerate(reader):
        d = {(k or "").strip(): (v or "").strip() for k, v in r.items() if k is not None}
        if not d.get("WarNum"):
            continue
        d["_line"] = str(i + 2)
        rows.append(d)
    if strict and len(rows) < MIN_ROWS[kind]:
        raise cd.drift(f"{FILES[kind]}: {len(rows)} rows < {MIN_ROWS[kind]} (truncated?)")
    versions = {r["Version"] for r in rows}
    if not versions <= {VERSIONS[kind], VERSIONS[kind].rstrip("0").rstrip(".")}:
        raise cd.drift(f"{FILES[kind]}: unexpected version {sorted(versions)}")
    return rows


def war_number(text: Any) -> str | None:
    """A COW war number as written (``502.1`` kept), ``None`` for -8 / empty; ``856.0`` -> ``856``."""
    s = str(text or "").strip()
    if not s or s in ("-8", "-9", "NA", "N/A"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f <= 0:
        return None
    return str(int(f)) if f == int(f) else s


def war_numbers(text: Any) -> list[str]:
    out = []
    for part in str(text or "").replace(";", ",").split(","):
        n = war_number(part)
        if n is not None:
            out.append(n)
    return out


def _spells(kind: str, r: Mapping[str, str]) -> list[tuple[tuple[str, int] | None, tuple[str, int] | None, bool]]:
    """(start, end, ongoing) per coded spell of one record."""
    if kind == "nonstate":
        keys = [("StartYear", "StartMonth", "StartDay", "EndYear", "EndMonth", "EndDay")]
    else:
        keys = [
            (f"StartYear{i}", f"StartMonth{i}", f"StartDay{i}", f"EndYear{i}", f"EndMonth{i}", f"EndDay{i}")
            for i in (1, 2)
        ]
    out = []
    for sy, sm, sd, ey, em, ed in keys:
        if r.get(sy, "") in ("", "-8"):
            continue
        start = cd.coded_date(r.get(sy), r.get(sm), r.get(sd), missing=MISSING)
        ongoing = r.get(ey, "") == "-7"
        end = None if ongoing else cd.coded_date(r.get(ey), r.get(em), r.get(ed), missing=MISSING)
        out.append((start, end, ongoing))
    return out


def _interval(
    kind: str, rows: Sequence[Mapping[str, str]]
) -> tuple[tuple[str, int] | None, tuple[str, int] | None, bool]:
    starts, ends, ongoing = [], [], False
    for r in rows:
        for s, e, og in _spells(kind, r):
            if s:
                starts.append(s)
            if e:
                ends.append(e)
            ongoing = ongoing or og
    start = min(starts, key=lambda x: x[0]) if starts else None
    end = None if ongoing or not ends else max(ends, key=lambda x: x[0])
    return start, end, ongoing


def _int(v: Any) -> int | None:
    s = str(v or "").strip()
    return int(s) if s.lstrip("-").isdigit() else None


# ----------------------------------------------------------------------------- KG + claims


def build_kg(data: Mapping[str, Sequence[Mapping[str, str]]], *, shas: Mapping[str, str]) -> dict[str, Any]:
    ents: dict[str, KgEntity] = {}
    sts: list[KgStatement] = []
    claims: dict[str, list[cd.ClaimRecord]] = {DATASETS[k]: [] for k in KINDS}
    anomalies: dict[str, Any] = defaultdict(list)
    known = {war_number(r["WarNum"]) for rows in data.values() for r in rows}
    trans: dict[tuple[str, str], list[str]] = defaultdict(list)

    for kind in KINDS:
        rows = data.get(kind, [])
        ds = DATASETS[kind]
        sha = shas[kind]
        f = FILES[kind]
        by_war: dict[str, list[Mapping[str, str]]] = defaultdict(list)
        for r in rows:
            n = war_number(r["WarNum"])
            if n is None:
                anomalies["bad_war_number"].append({"file": f, "line": r["_line"], "WarNum": r["WarNum"]})
                continue
            by_war[n].append(r)
        for n, wrows in sorted(by_war.items(), key=lambda x: float(x[0])):
            wid = f"cow:war:{n}"
            first = wrows[0]
            lines = [int(r["_line"]) for r in wrows]
            sel = {"file": f, "lines": lines[:50], "WarNum": n}
            name = first["WarName"]
            p = f"cw{n}"
            if kind in ("intra", "nonstate"):
                ents[wid] = KgEntity(
                    qid=wid,
                    kind="war",
                    labels={"en": name},
                    descriptions={
                        "en": f"{WAR_KIND_LABEL[kind]} #{n} (COW {WAR_KIND_LABEL[kind]} Data v{VERSIONS[kind]})"
                    },
                )
            wtype = cm.value_label(ds, "WarType", first["WarType"])
            if wtype:
                sts.append(
                    cm.string_stmt(
                        f"{p}:war_type",
                        wid,
                        "cowwar:war_type",
                        wtype,
                        raw_sha256=sha,
                        selector={**sel, "column": "WarType"},
                        code=first["WarType"],
                        war_family=WAR_KIND_LABEL[kind],
                    )
                )
            start, end, ongoing = _interval(kind, wrows)
            for s in cd.event_times(
                wid,
                f"{p}:t",
                start=start,
                end=end,
                raw_sha256=sha,
                selector={**sel, "columns": ["Start*", "End*"]},
                qualifiers={
                    "derived": "earliest coded start and latest coded end over the war's records (REVA summary of "
                    "the coded spells); no end when a spell is coded -7 (ongoing at the end of the data)",
                    "ongoing": ongoing or None,
                }
                if ongoing
                else {
                    "derived": "earliest coded start and latest coded end over the war's records (REVA summary of "
                    "the coded spells)",
                },
            ):
                sts.append(s)
            # sides and initiator (attributes / participant roles; never causal)
            if kind in ("extra", "intra"):
                for col, pid in (("SideA", "cowwar:side_a"), ("SideB", "cowwar:side_b")):
                    names = sorted({r[col] for r in wrows if r.get(col) and r[col] != "-8"})
                    for j, nm in enumerate(names):
                        sts.append(
                            cm.string_stmt(
                                f"{p}:{col.lower()}:{j}", wid, pid, nm, raw_sha256=sha, selector={**sel, "column": col}
                            )
                        )
            elif kind == "nonstate":
                for side, cols in (
                    ("a", ("SideA1", "SideA2")),
                    ("b", ("SideB1", "SideB2", "SideB3", "SideB4", "SideB5")),
                ):
                    for j, col in enumerate(cols):
                        v = first.get(col, "")
                        if v and v != "-8":
                            sts.append(
                                cm.string_stmt(
                                    f"{p}:side_{side}:{j}",
                                    wid,
                                    f"cowwar:side_{side}",
                                    v,
                                    raw_sha256=sha,
                                    selector={**sel, "column": col},
                                )
                            )
            if kind in ("intra", "nonstate") and first.get("Initiator") not in ("", "-8", "-9"):
                sts.append(
                    cm.string_stmt(
                        f"{p}:initiator",
                        wid,
                        "cowwar:initiator",
                        first["Initiator"],
                        raw_sha256=sha,
                        selector={**sel, "column": "Initiator"},
                        note="the participant that began the war (a role, not a cause)",
                    )
                )
            if kind == "intra":
                seen: set[tuple[str, str]] = set()
                for r in wrows:
                    for col, side in (("CcodeA", "A"), ("CcodeB", "B")):
                        c = _int(r.get(col))
                        if c is None or c <= 0 or (str(c), side) in seen:
                            continue
                        seen.add((str(c), side))
                        sts.append(
                            cm.item_stmt(
                                f"{p}:participant:{c}:{side}",
                                wid,
                                "P710",
                                f"cow:{c}",
                                raw_sha256=sha,
                                selector={"file": f, "line": int(r["_line"]), "WarNum": n, "column": col},
                                side=side,
                                state_name_in_source=r.get("Side" + side) or None,
                                role="state participant (COW intra-state war record)",
                            )
                        )
            # transformations (context)
            for r in wrows:
                for col in ("TransTo", "TransFrom"):
                    for m in war_numbers(r.get(col)):
                        a, b = (n, m) if col == "TransTo" else (m, n)
                        if m not in known:
                            anomalies["transformation_to_unknown_war"].append(
                                {"file": f, "line": r["_line"], "WarNum": n, col: m}
                            )
                            continue
                        trans[(a, b)].append(f"{f}:{r['_line']}:{col}")
            # outcome
            if kind == "inter":
                occ: Counter[str] = Counter()
                for r in wrows:
                    occ[r["ccode"]] += 1
                    _outcome(
                        r,
                        kind,
                        n,
                        name,
                        sha,
                        ents,
                        sts,
                        claims[ds],
                        anomalies,
                        suffix=f"{r['ccode']}:{occ[r['ccode']]}",
                        state=r.get("StateName"),
                        point=_interval(kind, [r])[1],
                    )
            else:
                codes = {r["Outcome"] for r in wrows}
                if len(codes) > 1:
                    anomalies["war_outcome_disagrees_between_records"].append(
                        {"file": f, "WarNum": n, "codes": sorted(codes)}
                    )
                    continue
                _outcome(
                    first,
                    kind,
                    n,
                    name,
                    sha,
                    ents,
                    sts,
                    claims[ds],
                    anomalies,
                    suffix=None,
                    state=None,
                    point=end,
                    lines=lines,
                )
    # transformation statements: one per (pair, file, coding column), with every coding row in the selector
    for (a, b), where in sorted(trans.items()):
        grouped: dict[tuple[str, str], list[int]] = defaultdict(list)
        for w in set(where):
            fname, line, col = w.rsplit(":", 2)
            grouped[(fname, col)].append(int(line))
        for (fname, col), lns in sorted(grouped.items()):
            kind = next(k for k, v in FILES.items() if v == fname)
            sts.append(
                cm.item_stmt(
                    f"cwt:{a}:{b}:{kind}:{col}",
                    f"cow:war:{a}",
                    "cowwar:transformed_into",
                    f"cow:war:{b}",
                    raw_sha256=shas[kind],
                    selector={"file": fname, "lines": sorted(lns), "column": col},
                    coded_by=col,
                    coded_on_war=b if col == "TransFrom" else a,
                    note="COW war transformation: the conflict changed category (context, not causal)",
                )
            )
    pairs = set(trans)
    report = {
        "transformation_pairs": len(pairs),
        "coded_by_both_wars": sum(
            1 for v in trans.values() if {x.rsplit(":", 1)[1] for x in v} >= {"TransTo", "TransFrom"}
        ),
    }
    return {
        "entities": list(ents.values()),
        "statements": sts,
        "claims": claims,
        "anomalies": {k: v for k, v in anomalies.items()},
        "transformations": report,
    }


def _outcome(
    r: Mapping[str, str],
    kind: str,
    n: str,
    war_name: str,
    sha: str,
    ents: dict[str, KgEntity],
    sts: list[KgStatement],
    claims: list[cd.ClaimRecord],
    anomalies: dict[str, Any],
    *,
    suffix: str | None,
    state: str | None,
    point: tuple[str, int] | None,
    lines: Sequence[int] | None = None,
) -> None:
    ds = DATASETS[kind]
    f = FILES[kind]
    code = _int(r.get("Outcome"))
    label = cm.value_label(ds, "Outcome", code)
    wid = f"cow:war:{n}"
    ln = [int(r["_line"])] if lines is None else list(lines)
    sel: dict[str, Any] = {"file": f, "lines": ln[:50], "WarNum": n, "column": "Outcome"}
    if suffix:
        sel["ccode"] = r.get("ccode")
    if label is None:
        anomalies["outcome_not_in_codebook"].append({"file": f, "WarNum": n, "Outcome": r.get("Outcome")})
        return
    key = f"cw{n}:outcome" + (f":{suffix}" if suffix else "")
    sts.append(
        cm.string_stmt(
            key,
            wid,
            "cowwar:outcome",
            label,
            raw_sha256=sha,
            selector=sel,
            code=str(code),
            state=state,
            ccode=r.get("ccode") if suffix else None,
            initiator_code=r.get("Initiator") if kind in ("inter", "extra") else None,
        )
    )
    if code not in OUTCOME_CLAIM_CODES:
        return  # 5: ongoing at the end of the data -- no outcome
    oid = f"cowwar:outcome:{n}" + (f":{suffix}" if suffix else "")
    who = f" for {state}" if state else ""
    ents[oid] = KgEntity(
        qid=oid,
        kind="war_outcome",
        labels={"en": f"Outcome of {war_name}{who}: {label}"},
        descriptions={
            "en": f"Coded outcome (COW Outcome = {code}) of COW war #{n}{who} "
            f"({WAR_KIND_LABEL[kind]} Data v{VERSIONS[kind]})"
        },
    )
    sts.append(
        cm.string_stmt(
            f"cwo{n}" + (f":{suffix}" if suffix else "") + ":type",
            oid,
            "causmore:event_type",
            "war_outcome",
            raw_sha256=sha,
            selector=sel,
        )
    )
    sts.append(
        cm.string_stmt(
            f"cwo{n}" + (f":{suffix}" if suffix else "") + ":outcome",
            oid,
            "cowwar:outcome",
            label,
            raw_sha256=sha,
            selector=sel,
            code=str(code),
        )
    )
    for s in cd.event_times(
        oid,
        f"cwo{n}" + (f":{suffix}" if suffix else "") + ":t",
        point=point,
        raw_sha256=sha,
        selector={**sel, "columns": ["End*"]},
        qualifiers={"definition": "dated at the latest coded end of combat of the record(s) (REVA summary)"},
    ):
        sts.append(s)
    claims.append(
        cd.ClaimRecord(
            cause_id=wid,
            effect_id=oid,
            relation="coded_outcome",
            selector={
                "file": f,
                "lines": ln[:50],
                "WarNum": n,
                "columns": ["Outcome"],
                **({"ccode": r.get("ccode")} if suffix else {}),
            },
            raw_sha256=sha,
            codebook_term="Outcome",
            source_claim_id=f"{n}:outcome" + (f":{suffix}" if suffix else ""),
            scope={"outcome_code": code, "outcome": label, **({"state": state} if state else {})},
        )
    )


# ----------------------------------------------------------------------------- atlas source + ingest


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Correlates of War Project (COW War Data, Sarkees and Wayman)",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(DATASET,),
        dataset_ids=tuple(f"cow-{k}-war-v{VERSIONS[k]}" for k in KINDS),
        cadence_seconds=90 * 86400,
        planned_requests=11,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={
            "kg": "cow:war:<n> (v4 numbering, shared with atlas.cow), cowwar:outcome:<n>[:<ccode>:<k>]",
            "claims": "registry/causal/relations_causmore.yaml datasets cow_inter, cow_extra, cow_intra, cow_nonstate",
            "context": "cowwar:transformed_into (TransTo/TransFrom), P710 (intra-state state participants)",
        },
        notes="KG-only source (no atlas table): atlas.cow already tabulates participation and battle deaths.",
    )


def build(raws: Mapping[str, Any], *, strict: bool = True) -> dict[str, Any]:
    shas = {k: hashlib.sha256(raws[k].body).hexdigest() for k in KINDS}
    data = {k: parse(k, raws[k].body, strict=strict) for k in KINDS}
    out = build_kg(data, shas=shas)
    cm.check_unique(out["entities"], out["statements"])
    return {
        **out,
        "shas": shas,
        "row_counts": {k: len(v) for k, v in data.items()},
        "wars": {k: len({war_number(r["WarNum"]) for r in v}) for k, v in data.items()},
    }


async def fetch_v51(run: Any) -> dict[str, Any]:
    """GET the intra-state v5.1 zip (numbering evidence, audit2 N8). A fetch failure is reported, never invented: the
    v4 war claims do not depend on it (in fixture mode a missing fixture is reported the same way)."""
    from reva.kernel.errors import SourceResponseError

    try:
        raw = await run.get(V51_URL, key="intra_v51_evidence", media_types=ZIP_MEDIA, max_bytes=MAX_BYTES)
    except SourceResponseError as e:
        return {"url": V51_URL, "status": "unavailable", "error": str(e)[:300]}
    return {
        "url": V51_URL,
        "status": "archived",
        "sha256": hashlib.sha256(raw.body).hexdigest(),
        "bytes": len(raw.body),
        "raw": raw,
    }


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the four war files and their codebooks, archive, load the KG records, deliver the claims."""
    if not run.wants(DATASET):
        return {"skipped": True}
    raws = {
        k: await run.get(file_url(k), key=f"{k}:{VERSIONS[k]}", media_types=MEDIA, max_bytes=MAX_BYTES) for k in KINDS
    }
    books: dict[str, dict[str, Any]] = {}
    for k in KINDS:
        raw = await run.get(codebook_url(k), key=CODEBOOKS[k], media_types=PDF_MEDIA, max_bytes=MAX_BYTES)
        books[k] = {"raw": raw, **cm.check_pdf(CODEBOOKS[k], raw.body)}
    v51 = await fetch_v51(run)
    out = build(raws, strict=run.origin == "live")
    for raw in [*raws.values(), *(b["raw"] for b in books.values())]:
        run.archive(raw)
    res: dict[str, Any] = {
        "row_counts": out["row_counts"],
        "wars": out["wars"],
        "anomalies": {
            k: (v if len(v) <= 30 else [*v[:30], f"... {len(v)} in total"]) for k, v in out["anomalies"].items()
        },
        "transformations": out["transformations"],
        "codebooks": {k: {x: v[x] for x in ("sha256", "matches")} for k, v in books.items()},
        "kg_counts": {"entities": len(out["entities"]), "statements": len(out["statements"])},
    }
    if v51.get("raw") is not None:
        raw51 = v51.pop("raw")
        run.archive(raw51)
        try:
            v51["check"] = v51_numbering_check(parse("intra", raws["intra"].body, strict=False), raw51.body)
        except cd.CausdataError as e:
            v51["check"] = {"error": str(e)}
    res["v51_numbering_evidence"] = v51
    retrieved = max(r.retrieved_at for r in raws.values())
    if cd.kg_available(run.engine):
        res["kg"] = cm.load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision=f"cow-wars-v4.0+intra-v4.1+reva-{VERSION}",
            raw_sha256s=sorted(set(out["shas"].values())),
            retrieved_at=retrieved,
            origin=run.origin,
        )
    deliveries = []
    for k in KINDS:
        if not books[k]["matches"]:
            res.setdefault("claims_withheld", {})[DATASETS[k]] = "codebook PDF differs from the pinned sha256"
            continue
        deliveries.append(
            cm.delivery(
                DATASETS[k],
                claims=out["claims"][DATASETS[k]],
                raw_sha256s=[out["shas"][k]],
                retrieved_at=retrieved,
                origin=run.origin,
                documents_sha256={CODEBOOKS[k]: books[k]["sha256"]},
                archived={CODEBOOKS[k]: books[k]["sha256"]},
            )
        )
    res["claims"] = cd.deliver_claims(run, deliveries)
    return res
