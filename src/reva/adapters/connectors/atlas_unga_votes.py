"""United Nations General Assembly roll-call votes (Voeten et al., Harvard Dataverse) -- agent KGGEOPOL.

Source and access
-----------------
Erik Voeten, Anton Strezhnev & Michael Bailey, "United Nations General Assembly Voting Data",
Harvard Dataverse ``doi:10.7910/DVN/LEJUQZ``, licence **CC0 1.0** (dataset terms). The raw roll-call
file is ``UNVotes-1.RData`` (file id 10295576, 8.7 MB, R data frame ``completeVotes``) published in
**dataset version 33.0** (2024-06-24); later versions (34-39, now titled "United Nations General
Assembly Ideal Points") dropped the raw votes and point to the UNGA-DM database
(https://unvotes.unige.ch/) for them. Files of earlier versions stay downloadable by file id, so the
connector pins version 33.0: it fetches the version's metadata (licence, file md5) and the RData file
(the access URL redirects to the Dataverse S3 bucket) and checks the md5 Dataverse publishes.
Coverage: roll calls 1946-01-01 .. 2024-06-04 (sessions 1-78 incl. emergency special sessions), 6,551 roll
calls, 1,283,746 country rows.
Votes after 2024-06-04 are **not** in this file (gap documented in docs/dev/v3_kggeopol.md).

Codebook (``Codebook-1.docx``, same version, archived too): vote 1 yes, 2 abstain, 3 no, 8 absent,
9 not a member; ``ccode`` = Correlates-of-War code; issue codes (ME Palestinian conflict, NU nuclear
weapons, DI arms control/disarmament, CO colonialism, HR human rights, EC economic development) "are
based on searches in descriptions ... subjected to a rudimentary visual check but may not be 100%
accurate"; since the 2019 release votes on amendments and paragraphs are included after 1990
(``amend``/``para``; often NA before).

Outputs
-------
* atlas ``unga.votes`` -- one row per (roll call, country) with a recorded position (yes / no /
  abstain / absent); "not a member" rows (vote 9) are not votes and are counted, not stored.
* atlas ``unga.rollcalls`` -- one row per roll call (provider_geo ``UNGA``, iso3 null): description
  text, symbol, date, issue codes, provider yes/no/abstain totals.
* atlas ``unga.vote_shares`` -- **computed** per country x period (calendar year, decade) x issue code
  (co, hr, me, nu, di, ec, all): counts of yes/no/abstain/absent and ``share_yes = yes / (yes + no +
  abstain)``; every row says it is computed from this file, with the formula and the issue-code caveat.
* KG: roll-call entities ``unga:rcid:<rcid>`` (date, symbol, short title, issue codes) and statements
  ``<state> unga:voted_yes|voted_no|voted_abstain unga:rcid:<rcid>`` at the vote date.

Provider defects handled explicitly (:func:`vote_cells`): (1) in 56 roll calls of 2023-24 a few rows
carry another roll call's description/symbol/date (misaligned rows); (2) some (roll call, country)
pairs have several rows -- COW 345 (Yugoslavia) 1999-2005 has three rows per roll call (the old seat
"absent" twice plus FR Yugoslavia's vote), a few 2023 rows are duplicated, one 2023 row has no ccode.
Identical duplicates collapse to one position (flagged ``duplicate_identical``); disagreeing rows give
no position (``conflicting``, ``missing: unreliable``, all provider values listed); misaligned-only
cells are kept flagged ``inconsistent``. Only clean cells feed shares and KG statements.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from reva.adapters.connectors.atlas_geopol_common import (
    COUNTRYCODE_VERSION,
    KgEntity,
    KgStatement,
    StateCodeCrosswalk,
    StateEntityResolver,
    atlas_frame,
    code_correspondence,
    fetch_countrycode,
    kg_available,
    load_kg,
    load_qid_for_iso3,
)
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

SOURCE_KEY = "harvard.dataverse.unvotes"
CONNECTOR_NAME = "atlas.unga_votes"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-unga-votes-parse-1"
ATLAS_SOURCE = "unga"
DOI = "doi:10.7910/DVN/LEJUQZ"
DATASET_VERSION = "33.0"
VOTES_FILE_ID = 10295576
VOTES_FILE_NAME = "UNVotes-1.RData"
CODEBOOK_FILE_ID = 10295577
HOSTS = ("dataverse.harvard.edu", "dvn-cloud-iqss.s3.amazonaws.com")
VERSION_URL = f"https://dataverse.harvard.edu/api/datasets/:persistentId/versions/{DATASET_VERSION}?persistentId={DOI}"
FILE_URL = "https://dataverse.harvard.edu/api/access/datafile/{}"
MAX_BYTES = 12_000_000
FRAME = "completeVotes"
COLUMNS = (
    "rcid",
    "ccode",
    "vote",
    "Country",
    "Countryname",
    "session",
    "importantvote",
    "date",
    "unres",
    "amend",
    "para",
    "short",
    "descr",
    "me",
    "nu",
    "di",
    "hr",
    "co",
    "ec",
    "resid",
    "yes",
    "no",
    "abstain",
)
VOTE = {1: "yes", 2: "abstain", 3: "no", 8: "absent"}
NOT_MEMBER = 9
ISSUES = ("co", "hr", "me", "nu", "di", "ec")
ISSUE_LABELS = {
    "me": "Palestinian conflict",
    "nu": "nuclear weapons and nuclear material",
    "di": "arms control and disarmament",
    "co": "colonialism",
    "hr": "human rights",
    "ec": "(economic) development",
}
ISSUE_CAVEAT = (
    "Voeten codebook: issue codes are based on searches in descriptions, subjected to a rudimentary "
    "visual check; they may not be 100% accurate"
)
MIN_ROWS = 1_200_000

RIGHTS: dict[str, str] = {
    "licence": "CC0 1.0",
    "licence_url": "http://creativecommons.org/publicdomain/zero/1.0",
    "attribution": "Voeten, Erik; Strezhnev, Anton; Bailey, Michael, 'United Nations General Assembly Voting Data', "
    "Harvard Dataverse, doi:10.7910/DVN/LEJUQZ, version 33.0 (UNVotes-1.RData)",
    "citation": "Bailey, Strezhnev & Voeten (2017), Estimating dynamic state preferences from United Nations "
    "voting data, Journal of Conflict Resolution 61(2): 430-456",
}


class UngaParseError(SourceResponseError):
    pass


def fetch_items() -> tuple[FetchItem, ...]:
    return (
        FetchItem(key="version", url=VERSION_URL, max_bytes=2_000_000, expected_media_types=("application/json",)),
        FetchItem(
            key="votes",
            url=FILE_URL.format(VOTES_FILE_ID),
            max_bytes=MAX_BYTES,
            expected_media_types=("application/x-rlang-transport", "application/octet-stream"),
        ),
        FetchItem(
            key="codebook",
            url=FILE_URL.format(CODEBOOK_FILE_ID),
            max_bytes=1_000_000,
            expected_media_types=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/octet-stream",
            ),
        ),
    )


def check_version(meta_body: bytes, votes_body: bytes) -> dict[str, Any]:
    """Licence and file md5 from the pinned dataset version; raises on drift."""
    doc = json.loads(meta_body)
    data = doc.get("data") or {}
    if f"{data.get('versionNumber')}.{data.get('versionMinorNumber')}" != DATASET_VERSION:
        raise UngaParseError("Dataverse returned another dataset version", retryable=False, kind="schema_drift")
    lic = (data.get("license") or {}).get("name")
    if lic != "CC0 1.0":
        raise UngaParseError(f"licence changed: {lic!r}", retryable=False, kind="licence_change")
    files = {f["dataFile"]["id"]: f["dataFile"] for f in data.get("files", [])}
    df = files.get(VOTES_FILE_ID)
    if df is None or df.get("filename") != VOTES_FILE_NAME:
        raise UngaParseError("votes file not in the pinned version", retryable=False, kind="schema_drift")
    md5 = hashlib.md5(votes_body, usedforsecurity=False).hexdigest()
    if df.get("md5") and df["md5"] != md5:
        raise UngaParseError("votes file md5 differs from Dataverse metadata", retryable=False, kind="checksum")
    return {"licence": lic, "md5": md5, "release_time": data.get("releaseTime"), "file_size": df.get("filesize")}


@dataclass
class Votes:
    """Columns of ``completeVotes`` as Python lists (row i = provider row i + 1)."""

    n: int
    cols: dict[str, list[Any]]


def parse_votes(body: bytes) -> Votes:
    from reva.adapters.connectors.atlas_rdata import RDataError, RealColumn, column_text, read_rdata_frame

    try:
        frame = read_rdata_frame(body, FRAME, COLUMNS)
    except RDataError as e:
        raise UngaParseError(f"UNVotes RData unreadable: {e}", retryable=False, kind="schema_drift") from e
    if frame.nrows < MIN_ROWS:
        raise UngaParseError(f"only {frame.nrows} rows (truncated?)", retryable=False, kind="incomplete")
    cols: dict[str, list[Any]] = {}
    for name, col in frame.columns.items():
        if name == "date":
            if not isinstance(col, RealColumn):
                raise UngaParseError("date column is not an R Date", retryable=False, kind="schema_drift")
            epoch = date(1970, 1, 1)
            cols[name] = [
                None if (col.na[i] or col.nan[i]) else epoch + timedelta(days=int(col.values[i]))
                for i in range(frame.nrows)
            ]
        elif isinstance(col, RealColumn) or name in ("rcid", "ccode", "importantvote"):
            vals = []
            for i in range(frame.nrows):
                t = column_text(col, i)
                vals.append(None if t is None else int(float(t)))
            cols[name] = vals
        else:
            cols[name] = [column_text(col, i) for i in range(frame.nrows)]
    return Votes(frame.nrows, cols)


@dataclass(frozen=True)
class RollCall:
    rcid: int
    date: date
    session: int | None
    unres: str | None
    resid: int | None
    short: str | None
    descr: str | None
    issues: tuple[str, ...]
    amend: int | None
    para: int | None
    important: int | None
    totals: dict[str, int | None]
    first_row: int
    inconsistent_rows: int


def _majority(c: Counter[Any]) -> Any:
    return sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]


def rollcalls(v: Votes) -> tuple[dict[int, RollCall], list[bool]]:
    """Roll-call metadata (majority value per rcid) and a per-row 'consistent with its roll call' mask."""
    c = v.cols
    keyf = ("date", "unres", "descr")
    counts: dict[int, dict[str, Counter[Any]]] = defaultdict(lambda: defaultdict(Counter))
    first: dict[int, int] = {}
    for i in range(v.n):
        r = c["rcid"][i]
        first.setdefault(r, i)
        for k in (
            *keyf,
            "session",
            "resid",
            "short",
            "amend",
            "para",
            "importantvote",
            "yes",
            "no",
            "abstain",
            *ISSUES,
        ):
            counts[r][k][c[k][i]] += 1
    maj = {r: {k: _majority(cnt) for k, cnt in d.items()} for r, d in counts.items()}
    ok = [all(c[k][i] == maj[c["rcid"][i]][k] for k in keyf) for i in range(v.n)]
    bad = Counter(c["rcid"][i] for i in range(v.n) if not ok[i])
    out = {}
    for r, m in maj.items():
        if m["date"] is None:
            raise UngaParseError(f"roll call {r} has no date", retryable=False)
        out[r] = RollCall(
            rcid=r,
            date=m["date"],
            session=m["session"],
            unres=m["unres"],
            resid=m["resid"],
            short=(m["short"] or "").strip() or None,
            descr=(m["descr"] or "").strip() or None,
            issues=tuple(k for k in ISSUES if m[k] == 1),
            amend=m["amend"],
            para=m["para"],
            important=m["importantvote"],
            totals={k: m[k] for k in ("yes", "no", "abstain")},
            first_row=first[r] + 1,
            inconsistent_rows=bad.get(r, 0),
        )
    return out, ok


@dataclass(frozen=True)
class VoteCell:
    """One (roll call, country) position after provider-defect handling.

    status: ``ok``; ``duplicate_identical`` (several provider rows, same position); ``conflicting``
    (consistent rows disagree -> no position); ``inconsistent`` (only misaligned rows, see module doc).
    Only ``ok`` / ``duplicate_identical`` cells feed shares and KG statements.
    """

    rcid: int
    ccode: int | None
    vote: int | None
    rows: tuple[int, ...]  # 1-based provider rows
    status: str
    provider_values: tuple[int, ...]
    country: str | None
    countryname: str | None
    row_unres: str | None = None

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "duplicate_identical") and self.vote in VOTE


def vote_cells(v: Votes, ok: Sequence[bool]) -> tuple[list[VoteCell], dict[str, int]]:
    c = v.cols
    groups: dict[tuple[int, int | None], list[int]] = defaultdict(list)
    stats: Counter[str] = Counter()
    for i in range(v.n):
        vote = c["vote"][i]
        if vote == NOT_MEMBER:
            stats["not_member_rows_skipped"] += 1
            continue
        if vote not in VOTE:
            stats["unknown_vote_code_rows_skipped"] += 1
            continue
        groups[(c["rcid"][i], c["ccode"][i])].append(i)
    cells = []
    for (rcid, ccode), idx in groups.items():
        good = [i for i in idx if ok[i]]
        votes = {c["vote"][i] for i in good}
        first = (good or idx)[0]
        if good and len(votes) == 1:
            status = "ok" if len(idx) == 1 else "duplicate_identical"
            vote: int | None = c["vote"][good[0]]
        elif good:
            status, vote = "conflicting", None
        else:
            status = "inconsistent"
            vote = c["vote"][idx[0]] if len({c["vote"][i] for i in idx}) == 1 else None
        stats[f"cells_{status}"] += 1
        cells.append(
            VoteCell(
                rcid,
                ccode,
                vote,
                tuple(i + 1 for i in idx),
                status,
                tuple(c["vote"][i] for i in idx),
                c["Country"][first],
                c["Countryname"][first],
                None if good else c["unres"][idx[0]],
            )
        )
    cells.sort(key=lambda x: (x.rcid, x.ccode if x.ccode is not None else -1))
    return cells, dict(stats)


# ----------------------------------------------------------------------------- atlas tables


def _period(d: date) -> tuple[date, date]:
    return d, d + timedelta(days=1)


def vote_rows(
    cells: Sequence[VoteCell], rcs: Mapping[int, RollCall], *, raw_sha256: str, crosswalk: StateCodeCrosswalk
) -> list[dict[str, Any]]:
    from reva.atlas.tables import json_obj

    out = []
    for cell in cells:
        rc = rcs[cell.rcid]
        res = crosswalk.resolve("cow", cell.ccode, rc.date)
        flags: dict[str, Any] = {"rcid": rc.rcid, "code_resolution": res.reason, "provider_country": cell.country}
        if cell.status != "ok":
            flags["provider_status"] = cell.status
            flags["provider_rows"] = list(cell.rows)
            flags["provider_values"] = [VOTE.get(x, x) for x in cell.provider_values]
        if cell.status == "inconsistent":
            flags["provider_row_inconsistent"] = (
                "row's description/symbol/date differ from the roll call's "
                "majority metadata; excluded from shares and KG"
            )
            flags["row_unres"] = cell.row_unres
        if res.iso3 is None and res.label:
            flags["historical_state"] = res.label
        value_text = VOTE[cell.vote] if cell.vote in VOTE and cell.status != "conflicting" else None
        if value_text is None:
            flags["missing"] = "unreliable"
        start, end = _period(rc.date)
        out.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": "votes",
                "indicator": f"vote|rcid={rc.rcid}",
                "concept_id": None,
                "iso3": res.iso3,
                "provider_geo": f"cow:{cell.ccode if cell.ccode is not None else 'NA'}",
                "entity_qid": None,
                "period_start": start,
                "period_end": end,
                "value": None,
                "value_text": value_text,
                "unit": "vote position",
                "flags": json_obj(flags),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=VOTES_FILE_NAME, object=FRAME, row=cell.rows[0], column="vote"),
            }
        )
    return out


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _clean_text(t: str) -> str:
    """Provider text with control characters (line breaks, tabs) folded to single spaces."""
    return re.sub(r"\s{2,}", " ", _CONTROL.sub(" ", t)).strip() or "?"


def rollcall_rows(rcs: Mapping[int, RollCall], *, raw_sha256: str) -> list[dict[str, Any]]:
    from reva.atlas.tables import MAX_TEXT, json_obj

    out = []
    for r in sorted(rcs):
        rc = rcs[r]
        start, end = _period(rc.date)
        flags = {
            "rcid": rc.rcid,
            "resid": rc.resid,
            "unres": rc.unres,
            "session": rc.session,
            "short": rc.short,
            "issue_codes": list(rc.issues),
            "amend": rc.amend,
            "para": rc.para,
            "importantvote": rc.important,
            "provider_totals": rc.totals,
            "provider_inconsistent_rows": rc.inconsistent_rows,
        }
        out.append(
            {
                "source": ATLAS_SOURCE,
                "dataset": "rollcalls",
                "indicator": f"rollcall|rcid={rc.rcid}",
                "concept_id": None,
                "iso3": None,
                "provider_geo": "UNGA",
                "entity_qid": None,
                "period_start": start,
                "period_end": end,
                "value": None,
                "value_text": _clean_text(rc.descr or rc.short or rc.unres or f"roll call {rc.rcid}")[:MAX_TEXT],
                "unit": "description",
                "flags": json_obj(flags),
                "raw_sha256": raw_sha256,
                "selector": json_obj(file=VOTES_FILE_NAME, object=FRAME, row=rc.first_row, column="descr"),
            }
        )
    return out


def share_rows(
    cells: Sequence[VoteCell], rcs: Mapping[int, RollCall], *, raw_sha256: str, crosswalk: StateCodeCrosswalk
) -> list[dict[str, Any]]:
    """Computed counts and yes-shares per (COW code, period, issue); see module docstring."""
    from reva.atlas.tables import json_obj, year_period

    # votes are grouped by the state the code denotes *at the vote date* (e.g. COW 365 = USSR until
    # 1991-12-20, then RUS): one group never mixes two states.
    acc: dict[tuple[int, str | None, str | None, str, int, str], Counter[str]] = defaultdict(Counter)
    cache: dict[tuple[int, date], Any] = {}
    for cell in cells:
        if not cell.usable or cell.ccode is None:
            continue
        rc = rcs[cell.rcid]
        k = (cell.ccode, rc.date)
        res = cache.get(k)
        if res is None:
            res = cache[k] = crosswalk.resolve("cow", cell.ccode, rc.date)
        y = rc.date.year
        for issue in (*rc.issues, "all"):
            for grain, p in (("year", y), ("decade", y // 10 * 10)):
                acc[(cell.ccode, res.iso3, res.label if res.iso3 is None else None, grain, p, issue)][
                    VOTE[cell.vote]
                ] += 1  # type: ignore[index]
    out = []
    for (ccode, iso3, hist, grain, p, issue), cnt in sorted(acc.items(), key=lambda kv: tuple(map(str, kv[0]))):
        if grain == "year":
            start, end = year_period(p)
        else:
            start, end = date(p, 1, 1), date(p + 10, 1, 1)
        res_reason = "resolved per vote date"
        n_rec = cnt["yes"] + cnt["no"] + cnt["abstain"]
        base: dict[str, Any] = {
            "computed_from": f"Voeten et al. UNGA votes, {DOI} v{DATASET_VERSION} ({VOTES_FILE_NAME})",
            "issue_code": issue,
            "issue_label": ISSUE_LABELS.get(issue, "all roll calls"),
            "grain": grain,
            "includes": "all roll calls (final passage, paragraph and amendment votes as in the file)",
            "code_resolution": res_reason,
        }
        if issue != "all":
            base["issue_code_caveat"] = ISSUE_CAVEAT
        if grain == "decade":
            base["decade"] = f"{p}s"
        if hist:
            base["historical_state"] = hist
        geo = f"cow:{ccode}" if iso3 is not None or not hist else f"cow:{ccode}|{hist}"
        vals: dict[str, tuple[str, str, dict[str, Any]]] = {
            f"n_{k}": (str(cnt[k]), "count", {}) for k in ("yes", "no", "abstain", "absent")
        }
        if n_rec:
            share = cnt["yes"] / n_rec
            vals["share_yes"] = (
                f"{share:.6f}".rstrip("0").rstrip(".") if share else "0",
                "ratio",
                {"formula": "n_yes / (n_yes + n_no + n_abstain); absences excluded", "n_recorded": n_rec},
            )
        for meas, (val, unit, extra) in vals.items():
            out.append(
                {
                    "source": ATLAS_SOURCE,
                    "dataset": "vote_shares",
                    "indicator": f"{meas}|issue={issue}|grain={grain}",
                    "concept_id": None,
                    "iso3": iso3,
                    "provider_geo": geo,
                    "entity_qid": None,
                    "period_start": start,
                    "period_end": end,
                    "value": val,
                    "value_text": None,
                    "unit": unit,
                    "flags": json_obj({**base, **extra}),
                    "raw_sha256": raw_sha256,
                    "selector": json_obj(
                        file=VOTES_FILE_NAME, object=FRAME, filter={"ccode": ccode, "issue": issue, grain: p}
                    ),
                }
            )
    return out


# ----------------------------------------------------------------------------- KG


def kg_records(
    cells: Sequence[VoteCell],
    rcs: Mapping[int, RollCall],
    *,
    raw_sha256: str,
    resolver: StateEntityResolver,
    issues_only: bool = False,
) -> tuple[list[KgEntity], list[KgStatement]]:
    """Roll-call entities + one vote statement per (state, roll call) with a yes/no/abstain position.

    ``issues_only`` restricts vote statements to issue-coded roll calls (smaller KG); the atlas keeps all.
    """
    ents: list[KgEntity] = []
    stmts: list[KgStatement] = []
    for r in sorted(rcs):
        rc = rcs[r]
        subj = f"unga:rcid:{r}"
        title = rc.short or rc.unres or f"roll call {r}"
        ents.append(
            KgEntity(
                qid=subj,
                kind="un_roll_call",
                labels={"en": _clean_text(f"UNGA roll call {r}: {title}")[:500]},
                descriptions={"en": _clean_text(rc.descr or title)[:500]},
            )
        )
        pit = rc.date.isoformat()
        sel = {"file": VOTES_FILE_NAME, "object": FRAME, "row": rc.first_row}
        if rc.unres:
            stmts.append(
                KgStatement(
                    key=f"{r}:symbol",
                    subject=subj,
                    property="unga:resolution_symbol",
                    value={"type": "string", "value": rc.unres},
                    point_in_time=pit,
                    pit_precision=11,
                    qualifiers={
                        "caveat": "before 1985 the number may not match the official "
                        "resolution number (Voeten codebook)"
                    },
                    raw_sha256=raw_sha256,
                    selector=sel,
                )
            )
        for issue in rc.issues:
            stmts.append(
                KgStatement(
                    key=f"{r}:issue:{issue}",
                    subject=subj,
                    property="unga:issue_code",
                    value={"type": "string", "value": issue},
                    qualifiers={"label": ISSUE_LABELS[issue], "caveat": ISSUE_CAVEAT},
                    raw_sha256=raw_sha256,
                    selector=sel,
                )
            )
        stmts.append(
            KgStatement(
                key=f"{r}:date",
                subject=subj,
                property="unga:vote_date",
                value={"type": "time", "time": pit, "precision": 11},
                qualifiers={
                    "session": rc.session,
                    "amend": rc.amend,
                    "para": rc.para,
                    "importantvote": rc.important,
                    "provider_totals": rc.totals,
                },
                raw_sha256=raw_sha256,
                selector=sel,
            )
        )
    for cell in cells:
        if not cell.usable or cell.vote not in (1, 2, 3) or cell.ccode is None:
            continue
        rc = rcs[cell.rcid]
        if issues_only and not rc.issues:
            continue
        subj = resolver.entity("cow", cell.ccode, rc.date, provider_label=cell.countryname)
        pos = VOTE[cell.vote]  # type: ignore[index]
        q: dict[str, Any] = {"ccode": cell.ccode}
        if cell.status != "ok":
            q["provider_status"] = cell.status
        stmts.append(
            KgStatement(
                key=f"{rc.rcid}:cow{cell.ccode}",
                subject=subj,
                property=f"unga:voted_{pos}",
                object_qid=f"unga:rcid:{rc.rcid}",
                point_in_time=rc.date.isoformat(),
                pit_precision=11,
                qualifiers=q,
                raw_sha256=raw_sha256,
                selector={"file": VOTES_FILE_NAME, "object": FRAME, "row": cell.rows[0], "column": "vote"},
            )
        )
    return ents, stmts


# ----------------------------------------------------------------------------- atlas source + ingest


def atlas_source() -> Any:
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Harvard Dataverse (Voeten, Strezhnev & Bailey, UNGA voting data)",
        licence=RIGHTS["licence"],
        allowed_hosts=(*HOSTS, "cran.r-project.org"),
        datasets=("votes", "rollcalls", "vote_shares"),
        dataset_ids=(f"{DOI}@{DATASET_VERSION}#{VOTES_FILE_ID}", f"countrycode-{COUNTRYCODE_VERSION}"),
        cadence_seconds=90 * 86400,
        planned_requests=6,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        field_mappings={"geo": "COW ccode", "time": "date of the roll call", "kg": "unga:rcid:<rcid>"},
        notes="Pinned dataset version 33.0 (the last version with the raw votes); md5 checked.",
    )


def build(
    raws: Mapping[str, RawAcquisition],
    *,
    crosswalk: StateCodeCrosswalk,
    qid_for_iso3: Mapping[str, str],
    kg_issues_only: bool = False,
    countrycode_sha256: str | None = None,
) -> dict[str, Any]:
    meta = check_version(raws["version"].body, raws["votes"].body)
    sha = hashlib.sha256(raws["votes"].body).hexdigest()
    v = parse_votes(raws["votes"].body)
    rcs, ok = rollcalls(v)
    cells, stats = vote_cells(v, ok)
    votes = vote_rows(cells, rcs, raw_sha256=sha, crosswalk=crosswalk)
    resolver = StateEntityResolver(crosswalk, qid_for_iso3)
    ents, stmts = kg_records(cells, rcs, raw_sha256=sha, resolver=resolver, issues_only=kg_issues_only)
    if countrycode_sha256:
        stmts = stmts + code_correspondence(resolver, raw_sha256=countrycode_sha256)
    return {
        "meta": meta,
        "sha": sha,
        "votes": votes,
        "rollcalls": rollcall_rows(rcs, raw_sha256=sha),
        "shares": share_rows(cells, rcs, raw_sha256=sha, crosswalk=crosswalk),
        "stats": stats,
        "entities": ents + resolver.code_entities(),
        "statements": stmts,
        "n_rows": v.n,
        "n_rollcalls": len(rcs),
        "date_range": [min(r.date for r in rcs.values()).isoformat(), max(r.date for r in rcs.values()).isoformat()],
        "unresolved_state_codes": {k: sorted(x) for k, x in resolver.unresolved.items()},
    }


async def atlas_ingest(run: Any) -> dict[str, Any]:
    crosswalk, cc_sha = await fetch_countrycode(run)
    raws = {}
    for it in fetch_items():
        raws[it.key] = await run.get(
            it.url, key=f"{it.key}:{DATASET_VERSION}", media_types=it.expected_media_types, max_bytes=it.max_bytes
        )
    out = build(raws, crosswalk=crosswalk, qid_for_iso3=load_qid_for_iso3(), countrycode_sha256=cc_sha)
    for raw in raws.values():
        run.archive(raw)
    note = (
        f"Voeten UNGA roll calls {out['date_range'][0]}..{out['date_range'][1]} ({out['n_rollcalls']} roll calls) "
        f"from {DOI} v{DATASET_VERSION}"
    )
    res: dict[str, Any] = {"stats": out["stats"], "meta": out["meta"], "atlas": {}}
    for ds, rows, extra in (
        ("votes", out["votes"], "; not-a-member rows skipped; provider defects flagged"),
        ("rollcalls", out["rollcalls"], "; provider_geo UNGA (not a country)"),
        ("vote_shares", out["shares"], "; COMPUTED counts and yes-shares (see flags)"),
    ):
        if run.wants(ds):
            r = run.register(
                dataset=ds, frame=atlas_frame(rows), raw_sha256s=[out["sha"]], rights=RIGHTS, coverage_note=note + extra
            )
            res["atlas"][ds] = {k: r[k] for k in ("dataset_key", "row_count", "created")}
            res["atlas"][ds]["unmapped_provider_geo"] = r["unmapped_provider_geo"][:40]
    res["kg_counts"] = {"entities": len(out["entities"]), "statements": len(out["statements"])}
    if run.dataset is None and kg_available(run.engine):
        res["kg"] = load_kg(
            run.engine,
            run.store,
            out["entities"],
            out["statements"],
            source_key=SOURCE_KEY,
            source_revision=f"dataverse-{DATASET_VERSION}",
            raw_sha256s=[out["sha"], cc_sha],
            retrieved_at=raws["votes"].retrieved_at,
            origin=run.origin,
        )
    return res
