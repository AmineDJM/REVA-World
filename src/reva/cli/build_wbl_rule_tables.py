"""Build retirement-age rule tables from World Bank WBL 2026 (V3 agent SIMWORLD; maintainer tool).

``uv run python -m reva.cli.build_wbl_rule_tables <WBL26 xlsx> [--retrieved-at ISO] [--write]``

Input: the World Bank *Women, Business and the Law 2026* data download
(``2026.02.19_WBL26_FINAL Data for Download.xlsx``, CC BY 4.0), sheet ``10. Pension``: per economy
the age at which a man / a woman can retire with **full pension benefits** (questions 10.1.1.1 and
10.1.1.2, law as of 1 October 2025, main business city) and WBL's ``Legal basis`` citation.

Output (with ``--write``):

* ``registry/rules/sources/WBL2026_pension_full_benefit_ages_excerpt.txt`` - verbatim cell values
  (sheet/cell references, the workbook's sha256 and URL in the header), the archived source every
  row's locator is checked against;
* ``registry/rules/retirement_age_wbl2026/<ISO3>.yaml`` - one rule table per economy whose ages are
  whole months, with two paths: ``wbl2026_in_force_2025`` (claim year 2025 only, verbatim) and
  ``wbl2026_held_constant`` (the same age for every claim year: an explicitly labelled
  counterfactual, since WBL does not state scheduled or past reforms), one track per sex;
* ``registry/rules/retirement_age_wbl2026/wbl2026_gaps.yaml`` - economies refused, with the reason.

The WBL tables live apart from the law-text tables (``registry/rules/retirement_age``): they are a
lower provenance tier and only ``social.retirement_age@1.1.0`` reads them.

Economies that already have a table transcribed from archived law text (FRA, DEU, ESP, NLD) are
never overwritten. A WBL age published with one decimal that is not a whole number of months
(e.g. 66.8) is refused: the month is not in the source. "NS" (no pension system) is refused.
Every table reads ``transcribed by agent SIMWORLD from WBL 2026, maintainer review pending``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from reva.adapters.connectors.atlas_xlsx import Workbook, col_index
from reva.models.social import rules as rl

URL = (
    "https://wbl.worldbank.org/content/dam/sites/wbl/documents/2026/2026.02.19_WBL26_FINAL%20Data%20for%20Download.xlsx"
)
SHEET = "10. Pension"
EXCERPT = "WBL2026_pension_full_benefit_ages_excerpt.txt"
SOURCE_ID = "wbl2026"
REPORT_YEAR = "2026"
AS_OF_YEAR = 2025  # WBL 2026 data are current as of 1 October 2025
KEEP = ("FRA", "DEU", "ESP", "NLD")  # tables transcribed from archived law text (agent PEN): never replaced
COLS = {"economy": "A", "iso": "C", "year": "F", "basis": "M", "men": "N", "women": "O"}
HEAD = {
    "men": "10.1.1.1 Ages men (full benefits)",
    "women": "10.1.1.2 Ages women (full benefits)",
    "basis": "Legal basis",
}
TRACKS = {"F": "women", "M": "men"}
#: WBL economy codes that are not country-crosswalk keys (registry/entities/country_crosswalk.json):
#: WBL "KSV" (Economy "Kosovo") is the crosswalk/WPP key "XKX". The excerpt keeps WBL's own code.
WBL_TO_CROSSWALK = {"KSV": "XKX"}


def _yaml_dump(doc: Any) -> str:
    class D(yaml.SafeDumper):
        def ignore_aliases(self, data: Any) -> bool:
            return True

    return yaml.dump(doc, Dumper=D, sort_keys=False, allow_unicode=True, width=110)


def read_rows(data: bytes) -> list[dict[str, Any]]:
    wb = Workbook(data)
    out = []
    header_ok = False
    for rnum, cells in wb.rows(SHEET):

        def g(letter: str, cells: Any = cells) -> str:
            c = cells.get(col_index(letter))
            return c.text if c is not None else ""

        if rnum == 3:
            got = {k: g(v) for k, v in COLS.items() if k in HEAD}
            if not all(got[k].startswith(HEAD[k]) for k in HEAD):
                raise SystemExit(f"header drift in {SHEET!r} row 3: {got}")
            header_ok = True
        if rnum < 4 or not g("C"):
            continue
        out.append({"row": rnum, **{k: g(v) for k, v in COLS.items()}})
    if not header_ok:
        raise SystemExit("no header row")
    return out


def months(text: str) -> int | None:
    """Whole months of a WBL age cell, or None when the cell is not a whole number of months."""
    try:
        d = Decimal(text)
    except InvalidOperation:
        return None
    m = d * 12
    return int(m) if m == m.to_integral_value() and 360 <= m <= 960 else None


def line(r: dict[str, Any], sex: str) -> str:
    col = COLS["men"] if sex == "M" else COLS["women"]
    head = HEAD["men"] if sex == "M" else HEAD["women"]
    value = r["men"] if sex == "M" else r["women"]
    return (
        f"[{SHEET}!{col}{r['row']}] ISO Code={r['iso']} | Economy={r['economy']} | Report Year={r['year']} | "
        f"{head}={value} | {HEAD['basis']} ({SHEET}!{COLS['basis']}{r['row']})={r['basis']}"
    )


def locator(r: dict[str, Any], sex: str) -> str:
    return line(r, sex).split(f" | {HEAD['basis']} (")[0]


def excerpt(rows: list[dict[str, Any]], sha: str, retrieved_at: str) -> str:
    head = [
        "World Bank, Women, Business and the Law 2026 - data download (CC BY 4.0)",
        f"URL: {URL}",
        f"Workbook sha256: {sha}",
        f"Retrieved: {retrieved_at} (by agent SIMWORLD)",
        f"Sheet: {SHEET!r}; data current as of 1 October {AS_OF_YEAR} (WBL 2026 report).",
        "Verbatim cell values, one line per economy and sex: ISO Code (column C), Economy (A), Report Year (F),",
        f"{HEAD['men']} (N) or {HEAD['women']} (O), and the Legal basis of question 10.1.1 (M).",
        "Excerpt made by agent SIMWORLD; the full workbook is not committed (hash above).",
        "",
    ]
    body = [line(r, s) for r in rows for s in ("M", "F")]
    return "\n".join(head + body) + "\n"


def table(r: dict[str, Any], retrieved_at: str, sha: str) -> dict[str, Any]:
    iso = WBL_TO_CROSSWALK.get(r["iso"], r["iso"])
    ages = {s: months(r["men"] if s == "M" else r["women"]) for s in ("F", "M")}
    rows: list[dict[str, Any]] = []
    for s, track in TRACKS.items():
        m = ages[s]
        assert m is not None
        age = {"years": m // 12, "months": m % 12}
        cit = {
            "source_id": SOURCE_ID,
            "legal_reference": f"{r['basis']} (legal basis cited by WBL 2026, question 10.1.1)",
            "locator": locator(r, s),
        }
        rows.append(
            {
                "row_id": f"{iso}.wbl2026.{track}.{AS_OF_YEAR}",
                "paths": ["wbl2026_in_force_2025", "wbl2026_held_constant"],
                "age_kind": "normal_pension_age",
                "basis": "claim_year",
                "track": track,
                "age": age,
                "year_from": AS_OF_YEAR,
                "year_to": AS_OF_YEAR,
                "citation": cit,
            }
        )
        for rid, yf, yt, when in (
            (f"{iso}.wbl2026.{track}.before", None, AS_OF_YEAR - 1, "every earlier claim year"),
            (f"{iso}.wbl2026.{track}.after", AS_OF_YEAR + 1, None, "every later claim year"),
        ):
            rows.append(
                {
                    "row_id": rid,
                    "paths": ["wbl2026_held_constant"],
                    "age_kind": "normal_pension_age",
                    "basis": "claim_year",
                    "track": track,
                    "age": age,
                    "year_from": yf,
                    "year_to": yt,
                    "derivation": "counterfactual_extension",
                    "note": f"Counterfactual: the WBL 2026 age (law as of 1 October {AS_OF_YEAR}) applied to {when}; "
                    "past and scheduled reforms are not captured by this source.",
                    "citation": cit,
                }
            )
    same = ages["F"] == ages["M"]
    return {
        "schema": rl.SCHEMA_ID,
        "version": "1.0.0",
        "review_status": rl.REVIEW_STATUS,
        "transcription": rl.WBL_TRANSCRIPTION_NOTE,
        "country": iso,
        "notes": (
            f"{r['economy']}: full-benefit retirement age {'for women and men ' if same else 'for men / women '}from "
            f"World Bank WBL 2026 (law as of 1 October {AS_OF_YEAR}), legal basis as cited by WBL: {r['basis']}. "
            + (f"WBL economy code {r['iso']} = crosswalk key {iso}. " if r["iso"] != iso else "")
            + "The legal text itself was not archived by REVA. WBL states one age at one date: the in-force path "
            f"covers claim year {AS_OF_YEAR} only; the held-constant path is an explicitly labelled counterfactual."
        ),
        "model_lever": {
            "age_kind": "normal_pension_age",
            "sex_tracks": dict(TRACKS),
            "rationale": (
                "WBL 2026 question 10.1.1: the age at which a woman / a man can retire with full pension benefits "
                "(general scheme, main business city); the only legal age REVA holds for this country."
            ),
            "qualification": (
                f"WBL 2026 full-benefit age as of 1 October {AS_OF_YEAR} held constant for every year (reforms "
                "before or after that date are not captured); early, partial, occupation-specific and "
                "contribution-record routes are not modelled; coverage of the pension system (informality) is "
                "not represented."
            ),
        },
        "sources": [
            {
                "source_id": SOURCE_ID,
                "title": "World Bank, Women, Business and the Law 2026 - sheet '10. Pension' (full-benefit retirement ages, legal basis)",
                "url": URL,
                "kind": "secondary_legal_dataset",
                "retrieved_at": retrieved_at,
                "response_sha256": sha,
                "archived_file": EXCERPT,
                "licence": "CC BY 4.0 (World Bank)",
                "note": "2.5 MB workbook not committed; verbatim cell excerpt committed (one line per economy and sex).",
            }
        ],
        "paths": [
            {
                "path_id": "wbl2026_in_force_2025",
                "label": f"WBL 2026: full-benefit age in force on 1 October {AS_OF_YEAR} (claim year {AS_OF_YEAR} only)",
                "status": "in_force",
                "description": "Verbatim WBL 2026 value; no other year is covered by the source.",
            },
            {
                "path_id": "wbl2026_held_constant",
                "label": "WBL 2026 age held constant for every claim year (counterfactual: reforms not captured)",
                "status": "counterfactual",
                "description": "The reference path for scenarios: the stated age, as if it applied in every year.",
            },
        ],
        "rows": rows,
        "claim_year_semantics": {
            "rule": "attained_once",
            "derivation": "model_assumption",
            "note": (
                "WBL gives one age at one date; a person who reached the age of a claim year stays eligible "
                "(stated model assumption, as for ESP)."
            ),
        },
    }


def build(xlsx: Path, retrieved_at: str, root: Path) -> dict[str, Any]:
    data = xlsx.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    rows = read_rows(data)
    tables: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    for r in rows:
        iso = WBL_TO_CROSSWALK.get(r["iso"], r["iso"])
        if r["year"] != REPORT_YEAR:
            raise SystemExit(f"{iso}: report year {r['year']} != {REPORT_YEAR}")
        if iso in KEEP:
            continue
        reasons = []
        for s, v in (("men", r["men"]), ("women", r["women"])):
            if v in ("NS", "N/A", ""):
                reasons.append(f"WBL 2026 {s}: {v!r} (no full-benefit pension age stated: 'NS' = no system)")
            elif months(v) is None:
                reasons.append(
                    f"WBL 2026 {s}: {v} years is rounded to one decimal and is not a whole number of months; "
                    "the month is not in the source (never guessed)"
                )
        if reasons:
            gaps.append({"country": iso, "source": "WBL 2026 (agent SIMWORLD)", "reason": "; ".join(reasons)})
            continue
        tables[iso] = table(r, retrieved_at, sha)
    return {"sha": sha, "rows": rows, "tables": tables, "gaps": gaps, "excerpt": excerpt(rows, sha, retrieved_at)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("xlsx", type=Path)
    ap.add_argument("--retrieved-at", default="2026-10-15T23:19:45Z")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    root = rl.RULES_ROOT
    out = build(args.xlsx, args.retrieved_at, root)
    print(json.dumps({"tables": len(out["tables"]), "gaps": len(out["gaps"]), "workbook_sha256": out["sha"]}))
    if not args.write:
        return 0
    (rl.SOURCE_DIR / EXCERPT).write_text(out["excerpt"], encoding="utf-8")
    for iso, doc in sorted(out["tables"].items()):
        header = (
            f"# {iso}: retirement-age rule table transcribed by agent SIMWORLD from World Bank WBL 2026\n"
            "# (src/reva/cli/build_wbl_rule_tables.py); maintainer review pending.\n"
        )
        rl.WBL_RULE_DIR.mkdir(parents=True, exist_ok=True)
        (rl.WBL_RULE_DIR / f"{iso}.yaml").write_text(header + _yaml_dump(doc), encoding="utf-8")
    gaps_doc = {
        "schema": "reva-retirement-age-gaps/1",
        "note": (
            "WBL 2026 economies without a rule table (agent SIMWORLD): the model refuses them with capability_gap. "
            "Countries with tables transcribed from archived law text (FRA, DEU, ESP, NLD) are listed in "
            "capability_gaps.yaml / their own files."
        ),
        "gaps": out["gaps"],
    }
    rl.WBL_GAPS_FILE.write_text(_yaml_dump(gaps_doc), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
