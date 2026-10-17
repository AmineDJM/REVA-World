"""Central-bank monetary-policy meeting calendars -> atlas table (V6, agent PRED-FIN).

The forecast registry asks "will the policy rate in force 7 days after the next scheduled decision be higher /
lower than today?" (``fin.*_hike`` / ``fin.*_cut`` families). The decision days come from the banks' own published
calendars, archived here with provenance to the raw page:

* **Federal Reserve Board** — ``https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm``: one panel per
  year (``"2026 FOMC Meetings"``), one entry per meeting with a month (``"October"``, ``"Jan/Feb"``) and days
  (``"27-28"``, ``"8-9*"`` — ``*`` = "Meeting associated with a Summary of Economic Projections"). The decision day
  is the **last day** of the meeting (the statement is released on it). Entries that are not a day range (e.g.
  ``"22 (notation vote)"``) are not scheduled policy meetings: counted in the report, no row.
* **European Central Bank** — ``https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html``: a list of
  ``dd/mm/yyyy`` entries; the decision day is an entry "Governing Council of the ECB: monetary policy meeting …
  (Day 2), followed by press conference", or a single-day "monetary policy meeting" entry not followed by a Day 2
  entry the next day. "non-monetary policy meeting" and General Council entries are not taken.
* **Bank of England** — ``https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates``: tables under
  "<year> confirmed dates", one row per MPC announcement (``"Thursday 5 November"``); the announcement day is the
  decision day.

Table ``cbcal.meetings``: indicator ``POLICY_DECISION|body=<FOMC|ECB_GC|BOE_MPC>``, ``provider_geo`` ``US`` /
``U2`` / ``GB`` (ISO alpha-2 crosswalk; ``U2`` = euro area stays unmapped), period ``[day, day + 1)``,
``value_text`` = the entry text verbatim, ``value`` null. ``flags.estimate_kind = "announced_schedule"``: a date
*as published on* ``flags.as_of`` (meetings can be rescheduled or added; an unscheduled meeting never appears here
in advance). Licence per row: Federal Reserve Board website "in the public domain" (cite the Board); ECB website
copyright (free use, cite the ECB); Bank of England website content "for personal use or internal use within an
individual organisation for non-commercial purposes" unless permission is granted — BoE rows carry
``flags.licence_restriction = "non_commercial_internal"`` (owner decision; the dates are facts, the page is not
openly licensed).
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.cb_calendars"
VERSION = "1.0.0"
PARSER_VERSION = "atlas_cb_calendars/1"
SOURCE_KEY = "cbcal.calendars"
ATLAS_SOURCE = "cbcal"
DATASETS = ("meetings",)
FED_HOST = "www.federalreserve.gov"
ECB_HOST = "www.ecb.europa.eu"
BOE_HOST = "www.bankofengland.co.uk"
HTML_MEDIA = ("text/html",)

FOMC_URL = f"https://{FED_HOST}/monetarypolicy/fomccalendars.htm"
ECB_URL = f"https://{ECB_HOST}/press/calendars/mgcgc/html/index.en.html"
BOE_URL = f"https://{BOE_HOST}/monetary-policy/upcoming-mpc-dates"
FED_TERMS_URL = f"https://{FED_HOST}/disclaimer.htm"
ECB_TERMS_URL = f"https://{ECB_HOST}/services/using-our-site/disclaimer/html/index.en.html"
BOE_TERMS_URL = f"https://{BOE_HOST}/legal"

FILES: tuple[hc.FileSpec, ...] = (
    hc.FileSpec("fomc_page", FOMC_URL, DATASETS, HTML_MEDIA, 3_000_000),
    hc.FileSpec("ecb_page", ECB_URL, DATASETS, HTML_MEDIA, 3_000_000),
    hc.FileSpec("boe_page", BOE_URL, DATASETS, HTML_MEDIA, 3_000_000),
    hc.FileSpec("fed_terms", FED_TERMS_URL, DATASETS, HTML_MEDIA, 2_000_000, role="policy"),
    hc.FileSpec("ecb_terms", ECB_TERMS_URL, DATASETS, HTML_MEDIA, 2_000_000, role="policy"),
    hc.FileSpec("boe_terms", BOE_TERMS_URL, DATASETS, HTML_MEDIA, 2_000_000, role="policy"),
)

_Q_FED = (
    "Unless otherwise indicated, information on Board's website is in the public domain and may be copied and "
    "distributed without permission. Please cite to the Board as the source of the information."
)
_Q_ECB = (
    "users of this website may make free use of the information obtained directly from it subject to the following "
    "conditions: When such information is distributed or reproduced, it must appear accurately and the ECB must be "
    "cited as the source."
)
_Q_BOE = (
    "download, display or print the Resources for personal use or internal use within an individual organisation "
    "for non-commercial purposes."
)
_Q_FOMC_SCHEDULE = "The FOMC holds eight regularly scheduled meetings during the year and other meetings as needed."
_Q_FOMC_SEP = "Meeting associated with a Summary of Economic Projections."

BODIES: dict[str, dict[str, str]] = {
    "FOMC": {
        "geo": "US",
        "page": "fomc_page",
        "licence": "Federal Reserve Board website: public domain unless otherwise indicated (cite the Board)",
        "attribution": "Source: Board of Governors of the Federal Reserve System",
        "decision_rule": "last day of the scheduled FOMC meeting (policy statement released that day)",
    },
    "ECB_GC": {
        "geo": "U2",
        "page": "ecb_page",
        "licence": "ECB website copyright: free use provided the information appears accurately and the ECB is cited",
        "attribution": "Source: European Central Bank (ECB)",
        "decision_rule": "Day 2 of a Governing Council monetary policy meeting (or a single-day monetary policy "
        "meeting), followed by the press conference",
    },
    "BOE_MPC": {
        "geo": "GB",
        "page": "boe_page",
        "licence": "Bank of England website: personal or internal non-commercial use unless the Bank grants "
        "permission (Database terms do not cover this page)",
        "attribution": "Source: Bank of England",
        "decision_rule": "MPC announcement day (Bank Rate decision published that day)",
        "licence_restriction": "non_commercial_internal",
    },
}

RIGHTS: dict[str, dict[str, Any]] = {
    "meetings": {
        "licence": "per row (flags.licence): Federal Reserve Board public domain; ECB website copyright (cite); "
        "Bank of England website content non-commercial internal use",
        "licence_url": FED_TERMS_URL,
        "terms_quote": _Q_FED,
        "attribution": "Sources: Board of Governors of the Federal Reserve System; European Central Bank; Bank of "
        "England",
        "licence_checked": "2026-10-17",
        "provider": "Federal Reserve Board, European Central Bank, Bank of England (meeting calendars)",
        "publication": "calendar pages updated by each bank (no schedule stated); the table is the calendar as "
        "fetched (flags.as_of)",
        "pub_lag": "not_stated",
        "revision_policy": "meetings can be rescheduled, added or cancelled by the banks; each fetch is a new "
        "version when the calendar changed",
        "licence_review": "Bank of England rows: website content licensed for non-commercial internal use only",
        "policy_quotes": {
            "fed_public_domain": ("fed_terms", _Q_FED),
            "ecb_copyright": ("ecb_terms", _Q_ECB),
            "boe_website_use": ("boe_terms", _Q_BOE),
        },
    }
}

#: verbatim calendar sentences checked against the archived calendar pages themselves
PAGE_QUOTES: dict[str, tuple[str, str]] = {
    "fomc_schedule": ("fomc_page", _Q_FOMC_SCHEDULE),
    "fomc_sep_mark": ("fomc_page", _Q_FOMC_SEP),
    "ecb_title": ("ecb_page", "Schedules for the meetings of the Governing Council and General Council of the ECB"),
    "boe_title": ("boe_page", "Dates for Monetary Policy Committee (MPC) announcements on Bank Rate"),
}

MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"), start=1)}  # fmt: skip
MONTH_ABBR = {k[:3]: v for k, v in MONTHS.items()}


def code_digest() -> str:
    return hc.code_digest(sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="Federal Reserve Board, European Central Bank, Bank of England (meeting calendars)",
        licence=RIGHTS["meetings"]["licence"],
        allowed_hosts=(FED_HOST, ECB_HOST, BOE_HOST),
        datasets=DATASETS,
        dataset_ids=("FRB:fomccalendars", "ECB:mgcgc", "BOE:upcoming-mpc-dates"),
        cadence_seconds=7 * hc.DAY,
        planned_requests=len(FILES),
        max_response_bytes=3_000_000,
        min_interval_seconds=2.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo_scheme": "iso2", "tables": ["cbcal.meetings"]},
        notes="three calendar pages + three terms pages (~0.6 MB) per run; weekly",
    )


def _clean(fragment: str) -> str:
    return hc.page_text(fragment.encode("utf-8"))


def _month(text: str) -> int | None:
    t = text.strip().lower()
    return MONTHS.get(t) or MONTH_ABBR.get(t[:3])


_DAYS = re.compile(r"^(\d{1,2})(?:-(\d{1,2}))?(\*?)$")


def parse_fomc(html_text: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Scheduled FOMC meetings: ``{day, text, year_panel, entry, sep}`` (decision day = last day)."""
    out: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    panels = list(re.finditer(r"<h4><a id=\"\d+\">(\d{4}) FOMC Meetings</a></h4>", html_text))
    if not panels:
        raise hc.err("fomc_page: no '<year> FOMC Meetings' panel")
    for i, p in enumerate(panels):
        year = int(p.group(1))
        body = html_text[p.end() : panels[i + 1].start() if i + 1 < len(panels) else len(html_text)]
        entries = re.findall(
            r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>\s*</div>\s*"
            r"<div class=\"fomc-meeting__date[^>]*>([^<]+)</div>",
            body,
        )
        for j, (mtxt, dtxt) in enumerate(entries):
            month_text, days_text = _clean(mtxt), _clean(dtxt)
            m = _DAYS.match(days_text.replace(" ", ""))
            months = [x for x in (_month(s) for s in month_text.split("/")) if x]
            if not m or not months:
                skipped[f"not a scheduled meeting day range: {days_text}"] += 1
                continue
            first, last = int(m.group(1)), int(m.group(2) or m.group(1))
            mon = months[-1] if (len(months) > 1 and last < first) else months[0]
            if len(months) > 1 and last >= first:
                mon = months[-1]
            try:
                d = date(year, mon, last)
            except ValueError as e:
                raise hc.err(f"fomc_page: bad date {year} {month_text} {days_text}") from e
            out.append({"day": d, "text": f"{year} FOMC Meetings: {month_text} {days_text}",
                        "selector": {"file": "fomc_page", "year_panel": year, "entry": j}, "sep": bool(m.group(3))})  # fmt: skip
    return out, skipped


def parse_ecb(html_text: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    """ECB Governing Council monetary-policy decision days (Day 2, or single-day meetings)."""
    rows = []
    for k, m in enumerate(re.finditer(r"<dt>\s*(\d{2}/\d{2}/\d{4})\s*</dt>\s*<dd>(.*?)</dd>", html_text, re.S)):
        d = datetime.strptime(m.group(1), "%d/%m/%Y").date()
        rows.append((d, _clean(m.group(2)), k))
    if not rows:
        raise hc.err("ecb_page: no dd/mm/yyyy entries")
    skipped: Counter[str] = Counter()
    mp = [(d, t, k) for d, t, k in rows if "monetary policy meeting" in t and "non-monetary" not in t]
    day2 = {d for d, t, _ in mp if "(Day 2)" in t}
    out: list[dict[str, Any]] = []
    for d, t, k in mp:
        if "(Day 1)" in t:
            continue
        if "(Day 2)" not in t and (d + timedelta(days=1)) in day2:
            skipped["first day of a two-day meeting without a Day 1 label"] += 1
            continue
        out.append({"day": d, "text": t, "selector": {"file": "ecb_page", "entry": k, "date": d.strftime("%d/%m/%Y")}})
    skipped["other entries (non-monetary policy, General Council, press)"] += len(rows) - len(mp)
    return out, skipped


def parse_boe(html_text: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Bank of England MPC announcement days from the "<year> confirmed dates" tables."""
    out: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    heads = list(re.finditer(r"<h2>\s*(\d{4}) confirmed dates\s*</h2>", html_text))
    if not heads:
        raise hc.err("boe_page: no '<year> confirmed dates' table")
    for i, h in enumerate(heads):
        year = int(h.group(1))
        body = html_text[h.end() : heads[i + 1].start() if i + 1 < len(heads) else len(html_text)]
        body = body[: body.find("</table>")] if "</table>" in body else body
        for j, tr in enumerate(re.findall(r"<tr>(.*?)</tr>", body, re.S)):
            cells = [_clean(c) for c in re.findall(r"<td>(.*?)</td>", tr, re.S)]
            if not cells:
                continue
            m = re.match(r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday)\s+(\d{1,2})\s+([A-Za-z]+)$", cells[0])
            if not m or _month(m.group(2)) is None:
                skipped[f"unparsed row: {cells[0][:40]}"] += 1
                continue
            mon = _month(m.group(2))
            assert mon is not None
            d = date(year, mon, int(m.group(1)))
            if d.strftime("%A") != cells[0].split()[0]:
                raise hc.err(f"boe_page: weekday mismatch for {cells[0]} {year}")
            out.append({"day": d, "text": " — ".join(cells),
                        "selector": {"file": "boe_page", "year_table": year, "row": j}})  # fmt: skip
    return out, skipped


PARSERS = {"FOMC": parse_fomc, "ECB_GC": parse_ecb, "BOE_MPC": parse_boe}


def build(
    files: Mapping[str, hc.Fetched], wanted: tuple[str, ...] = DATASETS
) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out = {ds: hc.TableBuild(hc.DailyRows(ATLAS_SOURCE, ds)) for ds in wanted}
    report: dict[str, Any] = {}
    if "meetings" in wanted:
        rows = out["meetings"].rows
        rights = RIGHTS["meetings"]
        for body, meta in BODIES.items():
            f = files[meta["page"]]
            found, skipped = PARSERS[body](f.text("utf-8"))
            seen: set[date] = set()
            for e in found:
                if e["day"] in seen:
                    raise hc.err(f"meetings: duplicate {body} decision day {e['day']}", kind="invalid")
                seen.add(e["day"])
                fl = rows.flags(**{
                    **hc.base_flags(rights, f), "licence": meta["licence"], "attribution": meta["attribution"],
                    "estimate_kind": "announced_schedule", "decision_rule": meta["decision_rule"],
                    "licence_restriction": meta.get("licence_restriction"),
                    "summary_of_economic_projections": True if e.get("sep") else None,
                    "note": "decision day as published on as_of; meetings can be rescheduled, unscheduled meetings "
                    "are not listed in advance",
                })  # fmt: skip
                rows.add_text(indicator=f"POLICY_DECISION|body={body}", provider_geo=meta["geo"],
                              span=hc.day_span(e["day"]), text=e["text"], unit="scheduled decision day", flags=fl,
                              raw_sha256=f.sha256, selector=e["selector"])  # fmt: skip
            report[body] = {
                "decision_days": len(found),
                "first": min(seen).isoformat() if seen else None,
                "last": max(seen).isoformat() if seen else None,
                "skipped": dict(skipped),
            }
    report["policy_quotes_found"] = hc.check_policy_pages(files, FILES, RIGHTS, wanted)
    report["page_quotes_found"] = hc.quotes_found(
        {k: files[k] for k in ("fomc_page", "ecb_page", "boe_page") if k in files}, PAGE_QUOTES
    )
    return out, report


NOTES = {
    "meetings": "Scheduled monetary-policy decision days of the FOMC (Federal Reserve Board calendar, last day of "
    "each meeting), the ECB Governing Council (Day 2 of monetary policy meetings) and the Bank of England MPC "
    "(announcement days), as published on flags.as_of; value_text = entry text verbatim; announced schedule, not "
    "an observation of a decision. BoE rows: non-commercial internal use (flags.licence_restriction).",
}


async def atlas_ingest(run: Any) -> dict[str, Any]:
    wanted = tuple(d for d in DATASETS if run.wants(d))
    files = await hc.fetch_all(run, FILES, wanted)
    builds, report = build(files, wanted)
    tables = hc.register_tables(run, builds, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY, geo_scheme="iso2")
    return {"ok": True, "tables": tables, "report": report}


__all__ = ["BODIES", "DATASETS", "FILES", "NAME", "RIGHTS", "atlas_ingest", "atlas_source", "build", "parse_boe",
           "parse_ecb", "parse_fomc"]  # fmt: skip
