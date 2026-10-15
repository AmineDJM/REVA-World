"""SSA "Social Security Programs Throughout the World" (SSPTW) 2018-2019 -> atlas table (V3, agent A-LAW).

Source: the US Social Security Administration's SSPTW (prepared with the ISSA), whose final regional
editions — Europe 2018, Asia and the Pacific 2018, Africa 2019, the Americas 2019 — are archived on
ssa.gov as one HTML page per country (``/policy/docs/progdesc/ssptw/2018-2019/<region>/<slug>.html``,
184 countries/territories). The series was not continued after 2018-2019. Access note (2026-10-15):
curl probes from this deployment got HTTP 403 (Akamai), the REVA client (httpx via the environment
proxy) gets 200.

Fetch plan (192 requests): per region the index page (the list of country pages) and the reading
guide (the edition's as-of statement, e.g. Europe: "the data reported are based on laws and
regulations in force in January 2018 or on the last date for which information has been received"),
then every country page linked from the index.

Cells (atlas table ``ssa.ssptw_2018_2019``; ``provider_geo`` = page slug, e.g. ``france``,
``congo-democratic-republic-of-the``; geo scheme ``ssptw`` of ``registry/atlas/geo_aliases/a_law.yaml``).
From the page's "Old Age, Disability, and Survivors" part, texts verbatim (HTML entities decoded,
whitespace collapsed, clipped at 3,000 characters with a flag):

* ``old_age.first_law``, ``old_age.current_laws``, ``old_age.type_of_program`` (Regulatory Framework);
* ``old_age.contributions.insured_person`` / ``.self_employed`` / ``.employer`` / ``.government``
  (Source of Funds: the paragraphs of each block — contribution rates);
* ``old_age.qualifying_conditions.first`` — the first paragraph of Qualifying Conditions (the first
  listed old-age program and its pensionable-age rule);
* ``old_age.qualifying_conditions`` — the whole Qualifying Conditions subsection.

Derived (``transcription = "transcribed by agent A-LAW from SSPTW <edition> <page>, maintainer review
pending"``; the source paragraph is kept in flags): ``old_age.pension_age.men`` / ``.women`` (years) or
``old_age.pension_age_months.men`` / ``.women`` (when stated as "N and M months"), read only from the
main clause (before the first ";") of that first paragraph, and only when that paragraph is an
"Old-age …" program, by :func:`pension_ages`:

* "Age N (men) or age M (women…)" (either order, optional "and K months") -> men N, women M;
* "Age N with … (men) or age M with … (women)" -> men N, women M;
* "Age N" with no men/women distinction in the main clause and no categorical alternative ages
  (a parenthesis right after the age may only start with "age", "gradually", "rising" or "if")
  -> the same age for men and women;
* anything else (France's "The legal minimum retirement age is 62…", occupation-specific ages,
  "flexible, from age 60 to age 65", ages in days) -> nothing derived; the text cells remain.

The age is the one stated for the edition's reference date; phasing schedules ("gradually rising to
age 67 by 2029") stay in the source text and are not projected. Period: the as-of month read from the
region's guide, ``[month start, next month start)``.
"""

from __future__ import annotations

import html
import re
import sys
from datetime import date
from typing import Any

from reva.adapters.connectors import atlas_law_common as common
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError

SOURCE_KEY = "ssa.ssptw"
ATLAS_SOURCE = "ssa"
ATLAS_DATASET = "ssptw_2018_2019"
CONNECTOR_NAME = "atlas.ssa_ssptw"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-ssa-ssptw-parse-1"
HOST = "www.ssa.gov"
BASE = f"https://{HOST}/policy/docs/progdesc/ssptw/2018-2019"
REGIONS = ("europe", "asia", "africa", "americas")
MAX_BYTES = 3_000_000
MIN_PAGES = {"europe": 40, "asia": 45, "africa": 45, "americas": 33}  # 45/54/50/38 on 2026-10-15
RIGHTS = {
    "licence": "US Government work (SSA), public domain in the US; prepared with the ISSA",
    "licence_url": "https://www.ssa.gov/policy/docs/progdesc/ssptw/",
    "attribution": "U.S. Social Security Administration and ISSA, Social Security Programs Throughout the World",
}
HTML_MEDIA = ("text/html",)

_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}
_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
}
_AS_OF = re.compile(r"based on laws and regulations in force in ([A-Z][a-z]+) (\d{4})")
_PAGE_LINK = re.compile(r'href="/policy/docs/progdesc/ssptw/2018-2019/([a-z]+)/([a-z0-9-]+)\.html"')
_AGE = r"(\d{2})(?: and (\w+) months?)?"
_PAIR = re.compile(rf"^Age {_AGE} \((men|women)\b[^)]*\) or age {_AGE} \((men|women)\b[^)]*\)")
_PAIR_WITH = re.compile(r"^Age (\d{2}) with [^;()]*\((men|women)\) or age (\d{2}) with [^;()]*\((men|women)\)")
_SINGLE = re.compile(rf"^Age {_AGE}(?=$|[\s.,;])")
_OK_PAREN = re.compile(r"^\((?:age|gradually|rising|if)\b")
_FIELDS = {
    "First law": "first_law",
    "Current laws": "current_laws",
    "Current law": "current_laws",
    "Type of program": "type_of_program",
}
_FUNDS = {
    "Insured person": "insured_person",
    "Self-employed person": "self_employed",
    "Employer": "employer",
    "Government": "government",
}


class SsptwParseError(SourceResponseError):
    pass


def _err(msg: str) -> SsptwParseError:
    return SsptwParseError(msg, retryable=False, kind="schema_drift")


def atlas_source() -> AtlasSourceSpec:
    """The ATLASCORE source spec (connector ``atlas.ssa_ssptw``; budget key ``ssa.ssptw``)."""
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="US Social Security Administration / ISSA",
        licence=RIGHTS["licence"],
        allowed_hosts=(HOST,),
        datasets=(ATLAS_DATASET,),
        dataset_ids=tuple(f"ssptw/2018-2019/{r}" for r in REGIONS),
        cadence_seconds=365 * 86400,
        planned_requests=192,
        max_response_bytes=MAX_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(sys.modules[__name__]),
        field_mappings={"edition": "2018-2019 (Europe/Asia 2018, Africa/Americas 2019)"},
    )


# ----------------------------------------------------------------------------- html helpers


def text_of(fragment: str) -> str:
    """Visible text of an HTML fragment: tags dropped, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def index_pages(body: bytes, region: str) -> list[str]:
    """Country page slugs linked from a region index (order kept, guide/index excluded)."""
    t = body.decode("utf-8", "replace")
    out: list[str] = []
    for reg, slug in _PAGE_LINK.findall(t):
        if reg == region and slug not in ("index", "guide") and slug not in out:
            out.append(slug)
    if len(out) < MIN_PAGES[region]:
        raise _err(f"{region} index lists {len(out)} country pages (< {MIN_PAGES[region]})")
    return out


def as_of_month(body: bytes, region: str) -> tuple[tuple[date, date], str]:
    """The guide's 'in force in <Month> <Year>' statement -> ([month start, next month start), quote)."""
    t = text_of(body.decode("utf-8", "replace"))
    m = _AS_OF.search(t)
    if not m or m.group(1) not in _MONTHS:
        raise _err(f"{region} guide: no 'in force in <Month> <Year>' statement")
    y, mo = int(m.group(2)), _MONTHS[m.group(1)]
    start = date(y, mo, 1)
    end = date(y + (mo == 12), mo % 12 + 1, 1)
    i = t.find(m.group(0))
    return (start, end), t[max(0, i - 60) : i + len(m.group(0)) + 80]


def _section(t: str, h2_word: str) -> str | None:
    h2 = list(re.finditer(r"<h2[^>]*>(.*?)</h2>", t, flags=re.S))
    for i, m in enumerate(h2):
        if h2_word in text_of(m.group(1)):
            return t[m.end() : h2[i + 1].start() if i + 1 < len(h2) else len(t)]
    return None


def _subsections(sec: str) -> dict[str, str]:
    parts = re.split(r"<h3[^>]*>(.*?)</h3>", sec, flags=re.S)
    return {text_of(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _paras(fragment: str) -> list[str]:
    return re.findall(r"<p[^>]*>(.*?)</p>", fragment, flags=re.S)


def _h4(p: str) -> str | None:
    m = re.match(r'\s*<span class="h4">(.*?)</span>', p, flags=re.S)
    return text_of(m.group(1)).rstrip(":").strip() if m else None


def _months(word: str | None) -> int | None:
    if word is None:
        return 0
    return int(word) if word.isdigit() else _WORDS.get(word.lower())


def pension_ages(paragraph: str) -> dict[str, Any] | None:
    """Ages from the first Qualifying Conditions paragraph (rules in the module doc), else None.

    Returns ``{"men": (years, months), "women": (years, months), "rule": name}``.
    """
    body = paragraph.split(":", 1)[1].strip() if ":" in paragraph else paragraph.strip()
    main = body.split(";", 1)[0]
    m = _PAIR.match(main)
    if m:
        a, am, sa, b, bm, sb = m.groups()
        ma, mb = _months(am), _months(bm)
        if {sa, sb} != {"men", "women"} or ma is None or mb is None:
            return None
        return {sa: (int(a), ma), sb: (int(b), mb), "rule": "age_men_or_women"}
    m = _PAIR_WITH.match(main)
    if m:
        a, sa, b, sb = m.groups()
        if {sa, sb} != {"men", "women"}:
            return None
        return {sa: (int(a), 0), sb: (int(b), 0), "rule": "age_with_men_or_women"}
    m = _SINGLE.match(main)
    if not m:
        return None
    if re.search(r"\b(men|women|male|female)\b", main, flags=re.I):
        return None
    rest = main[m.end() :].lstrip()
    if rest.startswith("(") and not _OK_PAREN.match(rest):
        return None  # categorical / occupational alternative ages
    if re.search(r"(?:,|\bor) age \d{2} \(", main):
        return None
    mo = _months(m.group(2))
    if mo is None:
        return None
    return {"men": (int(m.group(1)), mo), "women": (int(m.group(1)), mo), "rule": "single_age_no_sex_distinction"}


# ----------------------------------------------------------------------------- page parser


def parse_country_page(
    body: bytes, region: str, slug: str, raw_sha256: str, period: tuple[date, date], as_of_quote: str
) -> list[dict[str, Any]]:
    t = body.decode("utf-8", "replace")
    h1 = [text_of(x) for x in re.findall(r"<h1[^>]*>(.*?)</h1>", t, flags=re.S)]
    if len(h1) < 2 or not h1[0].startswith("Social Security Programs Throughout the World"):
        raise _err(f"{region}/{slug}: unexpected page headings {h1[:2]}")
    edition, country = h1[0].split(": ", 1)[-1], h1[1]
    file = f"2018-2019/{region}/{slug}.html"
    base = {
        "edition": edition,
        "country_name": country,
        "as_of": period[0].isoformat()[:7],
        "as_of_basis": f"SSPTW {edition} guide: '{as_of_quote}'",
        "licence": RIGHTS["licence"],
        "transcription": common.VERBATIM,
    }
    rows: list[dict[str, Any]] = []

    def add(
        indicator: str,
        selector: dict[str, Any],
        *,
        text: str | None = None,
        value: str | None = None,
        unit: str = "text",
        flags: dict[str, Any] | None = None,
    ) -> None:
        f = {**base, **(flags or {})}
        if text is not None:
            text, cut = common.clip_text(text, 3000)
            if cut:
                f["value_text_truncated"] = True
        rows.append(
            common.row(
                source=ATLAS_SOURCE,
                dataset=ATLAS_DATASET,
                indicator=indicator,
                provider_geo=slug,
                period=period,
                raw_sha256=raw_sha256,
                selector={"file": file, **selector},
                flags=f,
                unit=unit,
                value=value,
                value_text=text,
            )
        )

    sec = _section(t, "Old Age")
    if sec is None:
        return rows  # a page without an old-age part reports nothing for it
    subs = _subsections(sec)
    path = "Old Age, Disability, and Survivors"
    for i, p in enumerate(_paras(subs.get("Regulatory Framework", ""))):
        label = _h4(p)
        key = _FIELDS.get(label or "")
        if key:
            sel = {"section": f"{path} > Regulatory Framework", "paragraph": i, "label": label}
            add(f"old_age.{key}", sel, text=text_of(p).split(":", 1)[-1].strip())
    blocks: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    for i, p in enumerate(_paras(subs.get("Source of Funds", ""))):
        label = _h4(p)
        if label is not None:
            current = label
            rest = text_of(p)[len(label) :].lstrip(": ").strip()
            blocks.setdefault(current, [])
            if rest:
                blocks[current].append((i, rest))
        elif current is not None:
            blocks[current].append((i, text_of(p)))
    for label, paras in blocks.items():
        key = _FUNDS.get(label)
        if key and paras:
            sel = {"section": f"{path} > Source of Funds", "block": label, "paragraphs": [i for i, _ in paras]}
            add(f"old_age.contributions.{key}", sel, text=" ".join(x for _, x in paras))
    qc = _paras(subs.get("Qualifying Conditions", ""))
    if qc:
        first = text_of(qc[0])
        label = _h4(qc[0])
        sel = {"section": f"{path} > Qualifying Conditions", "paragraph": 0, "label": label}
        add("old_age.qualifying_conditions.first", sel, text=first)
        add(
            "old_age.qualifying_conditions",
            {"section": f"{path} > Qualifying Conditions", "paragraphs": len(qc)},
            text=" ".join(text_of(p) for p in qc),
        )
        ages = pension_ages(first) if (label or "").lower().startswith("old-age") else None
        if ages:
            for sex in ("men", "women"):
                years, months = ages[sex]
                f = {
                    "transcription": common.transcribed(f"SSPTW {edition} {file}"),
                    "rule": ages["rule"],
                    "program": label,
                    "source_text": first[:1500],
                }
                if months:
                    f["stated_as"] = f"{years} years and {months} months"
                    add(
                        f"old_age.pension_age_months.{sex}", sel, value=str(years * 12 + months), unit="months", flags=f
                    )
                else:
                    add(f"old_age.pension_age.{sex}", sel, value=str(years), unit="years", flags=f)
    return rows


# ----------------------------------------------------------------------------- ingest


async def atlas_ingest(
    run: Any, regions: tuple[str, ...] = REGIONS, slugs: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    """Region index + guide, then every country page -> atlas table ``ssa.ssptw_2018_2019``.
    ``regions``/``slugs`` restrict the run (tests). Unreadable pages are typed ``Blocked`` entries."""
    report = common.IngestReport(CONNECTOR_NAME)
    if not run.wants(ATLAS_DATASET):
        return report.as_dict()
    rows: list[dict[str, Any]] = []
    shas: list[str] = []
    for region in regions:
        got_idx = await common.fetch(
            run,
            f"{BASE}/{region}/index.html",
            key=f"{region}/index",
            source_key=SOURCE_KEY,
            media_types=HTML_MEDIA,
            max_bytes=MAX_BYTES,
        )
        got_guide = await common.fetch(
            run,
            f"{BASE}/{region}/guide.html",
            key=f"{region}/guide",
            source_key=SOURCE_KEY,
            media_types=HTML_MEDIA,
            max_bytes=MAX_BYTES,
        )
        if isinstance(got_idx, common.Blocked) or isinstance(got_guide, common.Blocked):
            report.blocked.extend(x for x in (got_idx, got_guide) if isinstance(x, common.Blocked))
            continue
        shas.extend([run.archive(got_idx), run.archive(got_guide)])
        period, quote = as_of_month(got_guide.body, region)
        pages = slugs.get(region, []) if slugs is not None else index_pages(got_idx.body, region)
        for slug in pages:
            got = await common.fetch(
                run,
                f"{BASE}/{region}/{slug}.html",
                key=f"{region}/{slug}",
                source_key=SOURCE_KEY,
                media_types=HTML_MEDIA,
                max_bytes=MAX_BYTES,
            )
            if isinstance(got, common.Blocked):
                report.blocked.append(got)
                continue
            sha = run.archive(got)
            shas.append(sha)
            rows.extend(parse_country_page(got.body, region, slug, sha, period, quote))
    report.raw.append({"archived": len(shas)})
    if rows:
        out = run.register(
            dataset=ATLAS_DATASET,
            frame=common.atlas_frame(rows),
            raw_sha256s=shas,
            geo_scheme="ssptw",
            rights=RIGHTS,
            coverage_note="SSPTW 2018-2019 country pages (old-age part); derived pension ages flagged; "
            f"not read: {[b.item for b in report.blocked][:40]}",
        )
        report.tables.append(common.table_summary(out))
    return report.as_dict()


__all__ = [
    "ATLAS_DATASET",
    "ATLAS_SOURCE",
    "CONNECTOR_NAME",
    "REGIONS",
    "as_of_month",
    "atlas_ingest",
    "atlas_source",
    "index_pages",
    "parse_country_page",
    "pension_ages",
]
