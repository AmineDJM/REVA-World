"""Olympic Games medal tables 1896-2026 (agent A-CULTURE): Wikipedia ``{{Medals table}}`` -> atlas + KG.

Source choice (licences checked 2026-10-16)
-------------------------------------------
* IOC results (olympics.com): site terms reserve all rights; no open data licence, no bulk download.
* Olympedia (olympedia.org, the OlyMADMen database the IOC uses): no licence stated anywhere on the site
  (about/FAQ pages) -> all rights reserved by default; not used. Kaggle "120 years of Olympic history" is a
  scrape of the same data without a licence from its owners -> not acceptable.
* Wikidata: no structured per-Games medal counts per NOC (only athlete-level P166 awards).
* **English Wikipedia** medal-table articles (``<year> <Summer|Winter> Olympics medal table``): text licence
  **CC BY-SA 4.0** (stated by the REST/page metadata of every article), each table cites the IOC's own
  medal table (olympics.com) or the Official Report in its caption reference, and the counts sit in one
  structured template, ``{{Medals table}}``, with one ``gold_<NOC>``/``silver_<NOC>``/``bronze_<NOC>`` triple
  per National Olympic Committee that won a medal. Community-edited: every cell is flagged
  ``community_edited_cites_ioc`` and keeps the page revision id.

Requests: the action API (``en.wikipedia.org/w/api.php``, GET, no key, no personal data), titles in batches
of :data:`BATCH` with ``prop=revisions|pageprops`` (content + revision id + the medal-table item QID), then
one ``prop=pageprops`` request for the Games articles (the Games' Wikidata QIDs for the KG). Wikimedia
rate-limits anonymous API traffic per IP (HTTP 429 seen 2026-10-16): a 429 ends the run (nothing registered
from partial data; retry later).

What a cell is
--------------
* ``provider_geo`` = the template's NOC code; when the template displays another code for it (``name_RUS =
  {{flagIOC|RU1|1912 Summer}}``: Russian Empire) the displayed code is used. ``iso3`` comes only from
  ``registry/atlas/geo_aliases/a_culture.yaml`` (scheme ``ioc_noc``); historical NOCs (URS, GDR, FRG, EUA,
  EUN, TCH, YUG, SCG, BOH, ANZ, RU1, ROC, OAR, AIN, ZZX mixed teams, IOP/IOA, EOR refugees, ...) stay
  unmapped -- never attributed to a successor.
* ``gold|season=<s>``, ``silver|...``, ``bronze|...`` = template values verbatim; ``total|...`` = their sum
  (``flags.aggregation = sum``, computed by REVA); ``games_host|...`` = 1 for the NOC(s) the template marks
  as host (``host`` = NOC code or ``host_<NOC> = yes``).
* Period = the calendar year the Games were held (Tokyo 2020 -> 2021, flagged with the edition).
* Only medal-winning NOCs appear in the template: a country without a row won no medal at those Games
  *or did not participate* -- never a reported zero.
* ``skip_<NOC>`` (listed outside the official ranking, e.g. AIN 2024) and ``note_<NOC>`` are kept in flags.

KG (``source_key wikipedia.olympic_medal_tables``): ``<Games QID> P17 <host country QID>`` for each host
NOC mapped to a country; ``<country QID or ioc:<code>> P1344 participant in <Games QID>`` for every NOC in
the medal table, with the medal counts as qualifiers (unmapped NOCs get an ``ioc:<code>`` entity).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from reva.kernel.errors import SourceResponseError

NAME = "atlas.olympics_medals"
VERSION = "1.0.0"
PARSER = "atlas-olympics-medals-1"
SOURCE_KEY = "wikipedia.olympic_medal_tables"
ATLAS_SOURCE = "olympics"
DATASET = "medal_tables"
HOST = "en.wikipedia.org"
API = f"https://{HOST}/w/api.php"
GEO_SCHEME = "ioc_noc"
BATCH = 10
MAX_BYTES = 8_000_000
RIGHTS: dict[str, str] = {
    "licence": "CC BY-SA 4.0",
    "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "attribution": "Wikipedia contributors, '<year> <season> Olympics medal table' (English Wikipedia), revision ids "
    "in each cell's flags; counts cite the International Olympic Committee",
    "share_alike": "derived tables must be shared under CC BY-SA 4.0",
    "source_key": SOURCE_KEY,
}

SUMMER = (1896, 1900, 1904, 1908, 1912, 1920, 1924, 1928, 1932, 1936, 1948, 1952, 1956, 1960, 1964, 1968, 1972,
          1976, 1980, 1984, 1988, 1992, 1996, 2000, 2004, 2008, 2012, 2016, 2020, 2024)  # fmt: skip
WINTER = (1924, 1928, 1932, 1936, 1948, 1952, 1956, 1960, 1964, 1968, 1972, 1976, 1980, 1984, 1988, 1992, 1994,
          1998, 2002, 2006, 2010, 2014, 2018, 2022, 2026)  # fmt: skip
HELD_IN = {(2020, "summer"): 2021}  # postponed edition

#: Historical / non-country teams (unmapped); names label their KG entities. Codes used for different
#: teams in different eras get one entity per era.
HISTORICAL: dict[str, tuple[tuple[int, int, str], ...]] = {
    "URS": ((1952, 1988, "Soviet Union"),),
    "EUN": ((1992, 1992, "Unified Team (former Soviet republics)"),),
    "GDR": ((1968, 1988, "East Germany (German Democratic Republic)"),),
    "FRG": ((1968, 1988, "West Germany (Federal Republic of Germany)"),),
    "EUA": ((1956, 1964, "United Team of Germany"),),
    "TCH": ((1920, 1992, "Czechoslovakia"),),
    "YUG": ((1920, 1992, "Yugoslavia"), (1996, 2002, "Federal Republic of Yugoslavia")),
    "SCG": ((2004, 2006, "Serbia and Montenegro"),),
    "BOH": ((1900, 1912, "Bohemia"),),
    "ANZ": ((1908, 1912, "Australasia"),),
    "RU1": ((1900, 1912, "Russian Empire"),),
    "ZZX": ((1896, 1924, "Mixed team (athletes of several nations)"),),
    "IOP": ((1992, 1992, "Independent Olympic Participants"),),
    "IOA": ((2000, 2016, "Independent Olympic Athletes"),),
    "ROC": ((1956, 1976, "Republic of China (IOC code ROC)"), (2020, 2022, "ROC (Russian Olympic Committee athletes)")),
    "OAR": ((2018, 2018, "Olympic Athletes from Russia"),),
    "AIN": ((2024, 2026, "Individual Neutral Athletes"),),
    "EOR": ((2016, 2026, "Refugee Olympic Team"),),
    "ROT": ((2016, 2016, "Refugee Olympic Team"),),
    "BWI": ((1960, 1960, "British West Indies Federation"),),
    "RAU": ((1960, 1960, "United Arab Republic"),),
    "AHO": ((1952, 2008, "Netherlands Antilles"),),
    "SAA": ((1952, 1952, "Saar"),),
    "MAL": ((1956, 1960, "Malaya"),),
    "RHO": ((1960, 1964, "Rhodesia"),),
    "VNM": ((1952, 1972, "South Vietnam (Republic of Vietnam)"),),
}  # fmt: skip


class OlympicsParseError(SourceResponseError):
    pass


def _err(msg: str) -> OlympicsParseError:
    return OlympicsParseError(msg, retryable=False, kind="schema_drift")


@dataclass(frozen=True)
class Games:
    year: int
    season: str  # "summer" | "winter"

    @property
    def edition(self) -> str:
        return f"{self.year} {self.season.capitalize()}"

    @property
    def table_title(self) -> str:
        return f"{self.edition} Olympics medal table"

    @property
    def article_title(self) -> str:
        return f"{self.edition} Olympics"

    @property
    def held(self) -> int:
        return HELD_IN.get((self.year, self.season), self.year)


GAMES: tuple[Games, ...] = tuple(Games(y, "summer") for y in SUMMER) + tuple(Games(y, "winter") for y in WINTER)
BY_TABLE = {g.table_title: g for g in GAMES}
BY_ARTICLE = {g.article_title: g for g in GAMES}


def _url(params: Mapping[str, str]) -> str:
    return f"{API}?{urlencode(params)}"


def table_urls(games: Sequence[Games] = GAMES, batch: int = BATCH) -> list[str]:
    titles = [g.table_title for g in games]
    return [
        _url({"action": "query", "format": "json", "formatversion": "2", "prop": "revisions|pageprops",
              "ppprop": "wikibase_item", "rvprop": "ids|timestamp|content", "rvslots": "main",
              "titles": "|".join(titles[i : i + batch])})
        for i in range(0, len(titles), batch)
    ]  # fmt: skip


def article_url(games: Sequence[Games] = GAMES) -> str:
    if len(games) > 50:
        raise ValueError("the action API takes at most 50 titles per request")
    return _url({"action": "query", "format": "json", "formatversion": "2", "prop": "pageprops",
                 "ppprop": "wikibase_item", "titles": "|".join(g.article_title for g in games)})  # fmt: skip


def article_urls(games: Sequence[Games] = GAMES) -> list[str]:
    return [article_url(games[i : i + 50]) for i in range(0, len(games), 50)]


# ----------------------------------------------------------------------------- wikitext parsing


def _template_block(text: str) -> str:
    m = list(re.finditer(r"\{\{\s*[Mm]edals[ _]table\b", text))
    if len(m) != 1:
        raise _err(f"expected exactly one {{{{Medals table}}}} template, found {len(m)}")
    i = j = m[0].start()
    depth = 0
    while j < len(text):
        if text.startswith("{{", j):
            depth += 1
            j += 2
            continue
        if text.startswith("}}", j):
            depth -= 1
            j += 2
            if depth == 0:
                return text[i + 2 : j - 2]
            continue
        j += 1
    raise _err("unterminated {{Medals table}} template")


def _strip(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>/]*/>", "", text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    return re.sub(r"</?(includeonly|noinclude|onlyinclude)>", "", text)


def template_params(block: str) -> dict[str, str]:
    """Top-level ``key = value`` parameters of a template body (pipes inside {{..}} / [[..]] ignored)."""
    body = _strip(block)
    parts, buf, depth_t, depth_l, k = [], [], 0, 0, 0
    while k < len(body):
        two = body[k : k + 2]
        if two == "{{":
            depth_t += 1
        elif two == "}}":
            depth_t -= 1
        elif two == "[[":
            depth_l += 1
        elif two == "]]":
            depth_l -= 1
        if two in ("{{", "}}", "[[", "]]"):
            buf.append(two)
            k += 2
            continue
        ch = body[k]
        if ch == "|" and depth_t == 0 and depth_l == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        k += 1
    parts.append("".join(buf))
    out: dict[str, str] = {}
    for p in parts[1:]:  # parts[0] is the template name
        if "=" not in p:
            continue
        key, val = p.split("=", 1)
        key = key.strip()
        if key in out and out[key].strip() != val.strip():
            raise _err(f"parameter {key!r} given twice with different values")
        out[key] = val.strip()
    return out


@dataclass(frozen=True)
class MedalRow:
    games: Games
    code: str  # template key
    geo: str  # provider_geo (displayed code when it differs)
    gold: int | None  # None: listed only as host (no medal triple in the template)
    silver: int | None
    bronze: int | None
    host: bool
    skip: bool
    note: bool


@dataclass
class MedalTable:
    games: Games
    page_title: str
    pageid: int
    revid: int
    timestamp: str
    table_qid: str | None
    host_param: str
    rows: list[MedalRow] = field(default_factory=list)


_CODE = re.compile(r"^[A-Z][A-Z0-9]{2}$")
_FLAG = re.compile(r"\{\{\s*flag\w*\s*\|\s*([A-Z][A-Z0-9]{2})\s*\|")


def parse_medal_table(games: Games, page: Mapping[str, Any]) -> MedalTable:
    try:
        rev = page["revisions"][0]
        text = rev["slots"]["main"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise _err(f"{games.table_title}: no revision content") from e
    params = template_params(_template_block(text))
    event = params.get("event", "").strip()
    if event and event != games.edition:
        raise _err(f"{games.table_title}: template event {event!r} != {games.edition!r}")
    codes = sorted({m.group(2) for k in params if (m := re.match(r"^(gold|silver|bronze)_(.+)$", k))})
    host_param = params.get("host", "")
    host_codes = {k[5:] for k, v in params.items() if k.startswith("host_") and v.strip().lower() == "yes"}
    if _CODE.match(host_param.strip()):
        host_codes.add(host_param.strip())
    table = MedalTable(
        games=games,
        page_title=str(page.get("title")),
        pageid=int(page["pageid"]),
        revid=int(rev["revid"]),
        timestamp=str(rev.get("timestamp")),
        table_qid=(page.get("pageprops") or {}).get("wikibase_item"),
        host_param=host_param[:200],
    )
    for code in codes:
        if not _CODE.match(code):
            raise _err(f"{games.table_title}: unexpected NOC key {code!r}")
        vals = {}
        for medal in ("gold", "silver", "bronze"):
            v = params.get(f"{medal}_{code}")
            if v is None or not re.fullmatch(r"\d{1,4}", v.strip()):
                raise _err(f"{games.table_title}: {medal}_{code} = {v!r} is not a count")
            vals[medal] = int(v.strip())
        disp = _FLAG.search(params.get(f"name_{code}", ""))
        geo = disp.group(1) if disp and disp.group(1) != code else code
        table.rows.append(
            MedalRow(
                games=games, code=code, geo=geo, gold=vals["gold"], silver=vals["silver"], bronze=vals["bronze"],
                host=code in host_codes, skip=params.get(f"skip_{code}", "").strip().lower() == "yes",
                note=bool(params.get(f"note_{code}", "").strip()),
            )
        )  # fmt: skip
    if not table.rows:
        raise _err(f"{games.table_title}: no medal rows")
    unknown_hosts = host_codes - set(codes)
    if unknown_hosts:  # a host that won nothing is still a host
        for c in sorted(unknown_hosts):
            table.rows.append(MedalRow(games, c, c, None, None, None, True, False, False))
    return table


def parse_table_response(body: bytes, *, expected: Sequence[Games]) -> list[MedalTable]:
    try:
        doc = json.loads(body)
        pages = doc["query"]["pages"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err("not a MediaWiki action API query response") from e
    if "continue" in doc:
        raise OlympicsParseError("API response is continued (truncated batch)", retryable=False, kind="incomplete")
    by_title = {p.get("title"): p for p in pages}
    out = []
    for g in expected:
        p = by_title.get(g.table_title)
        if p is None or p.get("missing") or p.get("invalid"):
            raise OlympicsParseError(f"page {g.table_title!r} missing", retryable=False, kind="incomplete")
        out.append(parse_medal_table(g, p))
    return out


def parse_article_qids(body: bytes) -> dict[Games, str]:
    try:
        pages = json.loads(body)["query"]["pages"]
    except (ValueError, KeyError, TypeError) as e:
        raise _err("not a MediaWiki action API query response") from e
    out = {}
    for p in pages:
        g = BY_ARTICLE.get(p.get("title"))
        q = (p.get("pageprops") or {}).get("wikibase_item")
        if g is not None and isinstance(q, str) and re.fullmatch(r"Q[1-9][0-9]*", q):
            out[g] = q
    return out


# ----------------------------------------------------------------------------- atlas rows


def historical_name(code: str, year: int) -> tuple[str, str] | None:
    """(entity suffix, name) of a historical / non-country team code in that year, if known."""
    for lo, hi, name in HISTORICAL.get(code, ()):
        if lo <= year <= hi:
            suffix = code if len(HISTORICAL[code]) == 1 else f"{code}.{lo}-{hi}"
            return suffix, name
    return None


def atlas_rows(tables: Sequence[MedalTable], *, raw_sha256s: Mapping[str, str]) -> list[dict[str, Any]]:
    """``raw_sha256s``: page title -> sha256 of the archived response that holds it."""
    from reva.atlas.tables import country_iso3

    rows: list[dict[str, Any]] = []
    for t in tables:
        g = t.games
        period = (date(g.held, 1, 1), date(g.held + 1, 1, 1))
        for r in t.rows:
            iso3 = country_iso3(r.geo, GEO_SCHEME)
            flags: dict[str, Any] = {
                "edition": g.edition,
                "page_revision": t.revid,
                "page": t.page_title,
                "verification": "community_edited_cites_ioc",
                "licence": RIGHTS["licence"],
            }
            if g.held != g.year:
                flags["held_in"] = g.held
            if r.geo != r.code:
                flags["template_key"] = r.code
            if r.skip:
                flags["outside_official_ranking"] = True
            if r.note:
                flags["template_note"] = True
            if iso3:
                flags["iso3_via"] = "IOC code continuity (territory at the time may differ)"
            else:
                h = historical_name(r.geo, g.year)
                flags["unmapped_noc"] = h[1] if h else "code not in the A-CULTURE IOC table"
            sel = {"page": t.page_title, "revid": t.revid, "template": "Medals table", "noc": r.code}
            if r.gold is not None and r.silver is not None and r.bronze is not None:
                vals = {"gold": r.gold, "silver": r.silver, "bronze": r.bronze}
                for medal, n in vals.items():
                    rows.append(_row(f"{medal}|season={g.season}", r.geo, period, str(n), flags,
                                     raw_sha256s[t.page_title], {**sel, "param": f"{medal}_{r.code}"}))  # fmt: skip
                rows.append(_row(f"total|season={g.season}", r.geo, period, str(sum(vals.values())),
                                 {**flags, "aggregation": "sum", "computed_by": "reva"}, raw_sha256s[t.page_title],
                                 {**sel, "param": "gold+silver+bronze"}))  # fmt: skip
            if r.host:
                rows.append(_row(f"games_host|season={g.season}", r.geo, period, "1",
                                 {**flags, "host_param": t.host_param}, raw_sha256s[t.page_title],
                                 {**sel, "param": "host"}))  # fmt: skip
    return rows


def _row(
    indicator: str,
    geo: str,
    period: tuple[date, date],
    value: str,
    flags: Mapping[str, Any],
    sha: str,
    sel: Mapping[str, Any],
) -> dict[str, Any]:
    stem, season = indicator.split("|season=")
    return {
        "source": ATLAS_SOURCE, "dataset": DATASET, "indicator": indicator,
        "concept_id": f"{CONCEPTS[stem][0]}_{season}", "iso3": None, "provider_geo": geo, "entity_qid": None, "period_start": period[0], "period_end": period[1],
        "value": value, "value_text": None, "unit": "count", "flags": _canon(flags), "raw_sha256": sha,
        "selector": _canon(sel),
    }  # fmt: skip


def _canon(o: Mapping[str, Any]) -> str:
    return json.dumps(dict(o), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


#: indicator stem -> (concept id stem, en, fr); concept id = "<stem>_<season>"
CONCEPTS: dict[str, tuple[str, str, str]] = {
    "gold": ("culture.olympic_gold_medals", "Olympic gold medals", "Médailles d'or olympiques"),
    "silver": ("culture.olympic_silver_medals", "Olympic silver medals", "Médailles d'argent olympiques"),
    "bronze": ("culture.olympic_bronze_medals", "Olympic bronze medals", "Médailles de bronze olympiques"),
    "total": ("culture.olympic_medals_total", "Olympic medals (total)", "Médailles olympiques (total)"),
    "games_host": ("culture.olympic_games_host", "Olympic Games host", "Pays hôte des Jeux olympiques"),
}


def frame(rows: Sequence[Mapping[str, Any]]) -> Any:
    import polars as pl

    from reva.atlas.tables import ATLAS_SCHEMA

    return pl.DataFrame(list(rows), schema=ATLAS_SCHEMA)


# ----------------------------------------------------------------------------- KG


@dataclass
class KgBuild:
    entities: list[Any] = field(default_factory=list)
    statements: list[Any] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def kg_build(
    tables: Sequence[MedalTable], *, games_qids: Mapping[Games, str], country_qids: Mapping[str, str],
    raw_sha256s: Mapping[str, str], retrieved: str,
) -> KgBuild:  # fmt: skip
    from reva.atlas.tables import country_iso3
    from reva.kg.model import KgEntity, KgStatement, KgTime

    out = KgBuild()
    ents: dict[str, Any] = {}
    for t in tables:
        g = t.games
        gq = games_qids.get(g)
        if gq is None:
            out.skipped.append({"games": g.edition, "reason": "no Wikidata QID for the Games article"})
            continue
        sha = raw_sha256s[t.page_title]
        ref = {"P854": f"https://{HOST}/w/index.php?oldid={t.revid}", "P813": retrieved,
               "publisher": "English Wikipedia (CC BY-SA 4.0), template {{Medals table}}; cites the IOC",
               "selector": {"page": t.page_title, "revid": t.revid}}  # fmt: skip
        rev = f"enwiki:{t.revid}"
        for r in t.rows:
            iso3 = country_iso3(r.geo, GEO_SCHEME)
            subj = country_qids.get(iso3) if iso3 else None
            if subj is None:
                if iso3:
                    out.skipped.append({"games": g.edition, "noc": r.geo, "reason": f"no QID for {iso3} in registry"})
                    continue
                h = historical_name(r.geo, g.year)
                suffix, name = h if h else (r.geo, f"IOC code {r.geo}")
                subj = f"ioc:{suffix}"
                ents.setdefault(subj, KgEntity(qid=subj, kind="olympic_team", labels={"en": f"{name} ({r.geo})"},
                                               descriptions={"en": "team code in Olympic medal tables (IOC)"},
                                               source_revision=rev))  # fmt: skip
            quals: dict[str, Any] = {"noc": r.geo, "listed_in": "medal table (medal-winning NOCs and hosts only)"}
            if r.gold is not None:
                quals.update(gold=str(r.gold), silver=str(r.silver), bronze=str(r.bronze))
            if r.skip:
                quals["outside_official_ranking"] = True
            out.statements.append(
                KgStatement(subject_qid=subj, property_pid="P1344", object_qid=gq, point_in_time=KgTime(f"{g.held:04d}", 9),
                            qualifiers=quals, references=(ref,), source_key=SOURCE_KEY, source_revision=rev,
                            statement_id=f"olympics:{g.year}{g.season[0]}:{r.geo}:participant", raw_sha256=sha)
            )  # fmt: skip
            if r.host and iso3 and country_qids.get(iso3):
                out.statements.append(
                    KgStatement(subject_qid=gq, property_pid="P17", object_qid=country_qids[iso3],
                                qualifiers={"host_noc": r.geo, "template_host": t.host_param},
                                references=(ref,), source_key=SOURCE_KEY, source_revision=rev,
                                statement_id=f"olympics:{g.year}{g.season[0]}:host:{r.geo}", raw_sha256=sha)
                )  # fmt: skip
    out.entities = list(ents.values())
    return out


def load_kg(run: Any, build: KgBuild, *, raw_sha256s: Sequence[str], retrieved_at: datetime) -> dict[str, Any]:
    from sqlalchemy.exc import DBAPIError

    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kernel.errors import RevaError
    from reva.kg.harvest import kg_properties
    from reva.kg.model import load_batch, register_properties

    try:
        register_properties(run.engine, [p for p in kg_properties(load_allowlist()) if p.pid in ("P17", "P1344")])
        rep = load_batch(run.engine, run.store, entities=build.entities, statements=build.statements,
                         source_key=SOURCE_KEY, raw_sha256s=sorted(set(raw_sha256s)), retrieved_at=retrieved_at,
                         origin=run.origin, parameters={"connector": NAME, "parser": PARSER})  # fmt: skip
    except RevaError as e:
        return {"status": "failed", "error": e.message, "details": e.details}
    except DBAPIError as e:
        return {"status": "failed", "error": f"{type(e.orig).__name__}: {str(e.orig)[:300]}"}
    d = rep.as_dict()
    d.update(status="loaded", statements_built=len(build.statements), skipped=build.skipped[:100])
    return d


# ----------------------------------------------------------------------------- protocol

#: Fixture mode replays these Games only (tests/fixtures/source/atlas.olympics_medals): Russian Empire (RU1,
#: 1912), ROC = Republic of China + BWI + RAU (1960), postponed Tokyo 2020 (ROC athletes), Paris 2024 (AIN).
FIXTURE_GAMES = (Games(1912, "summer"), Games(1960, "summer"), Games(2020, "summer"), Games(2024, "summer"))


def select_games(run: Any) -> tuple[Games, ...]:
    """All Games live; :data:`FIXTURE_GAMES` in fixture mode; option ``games=1960s,2026w`` narrows a run."""
    opt = (getattr(run, "options", None) or {}).get("games")
    if opt:
        items = opt if isinstance(opt, list) else str(opt).split(",")
        want = []
        for it in items:
            m = re.fullmatch(r"(\d{4})([sw])", str(it).strip())
            g = m and Games(int(m.group(1)), "summer" if m.group(2) == "s" else "winter")
            if not g or g not in GAMES:
                raise ValueError(f"unknown Games {it!r} (format 1960s / 2026w)")
            want.append(g)
        return tuple(g for g in GAMES if g in want)
    return FIXTURE_GAMES if run.origin == "test_fixture" else GAMES


def atlas_source() -> Any:
    from reva.adapters.connectors.base import module_digest
    from reva.atlas.sources import AtlasSourceSpec

    n_req = len(table_urls()) + len(article_urls())
    return AtlasSourceSpec(
        name=NAME, version=VERSION, source_key=SOURCE_KEY, atlas_source=ATLAS_SOURCE,
        provider="English Wikipedia Olympic medal tables (template {{Medals table}}; counts cite the IOC)",
        licence=f"{RIGHTS['licence']} ({RIGHTS['licence_url']}); attribution: Wikipedia contributors",
        allowed_hosts=(HOST,), datasets=(DATASET,), dataset_ids=tuple(g.table_title for g in GAMES),
        cadence_seconds=30 * 86400, planned_requests=n_req, max_response_bytes=MAX_BYTES,
        min_interval_seconds=10.0, parser_version=PARSER, code_digest=module_digest(atlas_source),
        field_mappings={"geo": "NOC code (scheme ioc_noc)", "kg": ["P17", "P1344"]},
        notes="55 Games (Summer 1896-2024, Winter 1924-2026); medal-winning NOCs only; historical NOCs unmapped.",
    )  # fmt: skip


async def atlas_ingest(run: Any) -> dict[str, Any]:
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    if not run.wants(DATASET):
        return {"skipped": DATASET}
    games = select_games(run)
    raws, tables = [], []
    for i, url in enumerate(table_urls(games)):
        raw = await run.get(url, key=f"medal_tables:{i}", max_bytes=MAX_BYTES)
        tables += parse_table_response(raw.body, expected=games[i * BATCH : (i + 1) * BATCH])
        raws.append(raw)
    art_raws, qids = [], {}
    for i, url in enumerate(article_urls(games)):
        raw = await run.get(url, key=f"games_articles:{i}", max_bytes=1_000_000)
        qids.update(parse_article_qids(raw.body))
        art_raws.append(raw)
    shas = [run.archive(r) for r in raws]
    art_shas = [run.archive(r) for r in art_raws]
    page_sha = {t.page_title: shas[i // BATCH] for i, t in enumerate(tables)}
    rows = atlas_rows(tables, raw_sha256s=page_sha)
    reg = run.register(
        dataset=DATASET, frame=frame(rows), raw_sha256s=shas, geo_scheme=GEO_SCHEME, rights=dict(RIGHTS),
        coverage_note=(f"Olympic medal tables of {len(tables)} Games (English Wikipedia {{{{Medals table}}}}, "
                       "CC BY-SA 4.0, citing the IOC); medal-winning NOCs only; historical NOCs unmapped"),
    )  # fmt: skip
    build = kg_build(tables, games_qids=qids, country_qids=load_country_qids(), raw_sha256s=page_sha,
                     retrieved=raws[0].retrieved_at.date().isoformat())  # fmt: skip
    kg = load_kg(run, build, raw_sha256s=shas, retrieved_at=max(r.retrieved_at for r in raws))
    return {
        "games": len(tables),
        "noc_rows": sum(len(t.rows) for t in tables),
        "revisions": {t.page_title: t.revid for t in tables},
        "games_without_qid": [g.edition for g in games if g not in qids],
        "article_raws": art_shas,
        "atlas": {k: reg[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created",
                                      "unmapped_provider_geo")},
        "kg": kg,
    }  # fmt: skip
