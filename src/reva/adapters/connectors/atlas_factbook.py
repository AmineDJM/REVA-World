"""CIA World Factbook legal fields -> atlas table ``factbook.legal_2026_01`` (V3, agent A-LAW).

Source: the CC0 mirror ``factbook/factbook.json`` of the public-domain CIA World Factbook (the CIA
discontinued the Factbook in February 2026). The mirror's last data update is commit ``783ed27``
("auto-update week 4", 2026-01-22T22:32Z); later commits only move two files between region
folders. Profiles are fetched one JSON file per entity (255 files, ~60 kB each) from
``raw.githubusercontent.com`` at the pinned commit :data:`COMMIT` (bytes cannot change).

Fields taken (``Government`` section, texts verbatim):

* ``legal_system`` — "Legal system" (e.g. "civil law; review of administrative but not legislative acts");
* ``suffrage`` — "Suffrage" (e.g. "18 years of age; universal");
* ``constitution.history`` / ``constitution.amendment_process``;
* ``citizenship.by_birth``, ``citizenship.by_descent_only``, ``citizenship.dual_recognized``,
  ``citizenship.naturalization_residency``;
* ``international_law_participation`` — "International law organization participation" (ICJ / ICCt).

Two agent-derived indicators, each flagged ``transcription = "transcribed by agent A-LAW from …,
maintainer review pending"`` and keeping the source text in flags:

* ``suffrage.minimum_age`` — the leading "<N> years of age" of the suffrage text, only when the text
  starts with it (e.g. "18 years of age; universal" -> 18; "18-65 years of age" -> not derived);
* ``legal_system.families_mentioned`` — the legal-family keywords the legal-system text mentions
  (:data:`FAMILY_KEYWORDS`: civil, common, customary, islamic, religious, socialist), a mention
  list — not a classification of the legal system.

Period: the as-published day of the last data update ``[2026-01-22, 2026-01-23)``; the Factbook's
own per-field revision dates are not in the mirror. Geography: ``provider_geo`` = the Factbook GEC
code (file name, upper case), mapped through ATLASCORE's ``fips`` scheme (GEC codes are the FIPS
10-4 successors); dependencies, Antarctica, the EU, Gaza Strip, Taiwan… stay unmapped (kept, listed).
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from datetime import date
from typing import Any

from reva.adapters.connectors import atlas_law_common as common
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import ConnectorSpec, FetchItem

SOURCE_KEY = "factbook.json"
ATLAS_SOURCE = "factbook"
ATLAS_DATASET = "legal_2026_01"
CONNECTOR_NAME = "atlas.factbook"
CONNECTOR_VERSION = "1.0.0"
PARSER_VERSION = "atlas-factbook-parse-1"
HOST = "raw.githubusercontent.com"
COMMIT = "144d6977b2b01ac1cbd220de754c0a005616760b"  # data identical to 783ed27 (2026-01-22)
DATA_COMMIT = "783ed27f7a1116663c10e7b39d162d0c4500505d"
AS_PUBLISHED = (date(2026, 1, 22), date(2026, 1, 23))
MAX_BYTES = 600_000
RIGHTS = {
    "licence": "Public domain (US Government work); mirror CC0-1.0",
    "licence_url": "https://github.com/factbook/factbook.json/blob/master/LICENSE.md",
    "attribution": "The World Factbook, Central Intelligence Agency (via factbook/factbook.json)",
}

#: region folder -> GEC codes (from the mirror tree at COMMIT; 'meta', 'oceans', 'world' excluded).
PATHS: dict[str, str] = {
    "africa": "ag ao bc bn by cd cf cg cm cn ct cv dj eg ek er et ga gb gh gv iv ke li lt ly ma mi ml mo mp mr mz "
    "ng ni od pu rw se sf sg sh sl so su to tp ts tz ug uv wa wi wz za zi",
    "antarctica": "ay bv fs hm",
    "australia-oceania": "aq as at bp ck cq cr cw fj fm fp gq kr kt nc ne nf nh nr nz pc ps rm tl tn tv um wf wq ws",
    "central-america-n-caribbean": "aa ac av bb bf bh bq cj cs cu do dr es gj gt ha ho jm mh nn nu pm rn rq sc st "
    "tb td tk uc vc vi vq",
    "central-asia": "aj am gg kg kz rs ti tx uz",
    "east-n-southeast-asia": "bm bx cb ch hk id ja kn ks la mc mg my pf pg pp rp sn th tt tw vm",
    "europe": "al an au ax be bk bo bu cy da dx ee ei en ez fi fo fr gi gk gm gr hr hu ic im it je jn kv lg lh lo "
    "ls lu md mj mk mn mt nl no pl po ri ro si sm sp sv sw sz uk up vt",
    "middle-east": "ae ba gz ir is iz jo ku le mu qa sa sy tu we ym",
    "north-america": "bd ca gl ip mx sb us",
    "south-america": "ar bl br ci co ec fk gy ns pa pe sx uy ve",
    "south-asia": "af bg bt ce in io mv np pk",
}

#: indicator -> (path of keys inside the profile, unit)
TEXT_FIELDS: dict[str, tuple[tuple[str, ...], str]] = {
    "legal_system": (("Government", "Legal system"), "text"),
    "suffrage": (("Government", "Suffrage"), "text"),
    "constitution.history": (("Government", "Constitution", "history"), "text"),
    "constitution.amendment_process": (("Government", "Constitution", "amendment process"), "text"),
    "citizenship.by_birth": (("Government", "Citizenship", "citizenship by birth"), "text"),
    "citizenship.by_descent_only": (("Government", "Citizenship", "citizenship by descent only"), "text"),
    "citizenship.dual_recognized": (("Government", "Citizenship", "dual citizenship recognized"), "text"),
    "citizenship.naturalization_residency": (
        ("Government", "Citizenship", "residency requirement for naturalization"),
        "text",
    ),
    "international_law_participation": (("Government", "International law organization participation"), "text"),
}
FAMILY_KEYWORDS: dict[str, str] = {
    "civil": r"\bcivil\b",
    "common": r"\bcommon\b",
    "customary": r"\bcustomary\b",
    "islamic": r"\b(islamic|sharia|shari'a)\b",
    "religious": r"\b(religious|hindu|talmudic|jewish|canon|ecclesiastical)\b",
    "socialist": r"\b(socialist|soviet|communist)\b",
}
_AGE = re.compile(r"^\s*(\d{2}) years of age\b")
_TAGS = re.compile(r"<[^>]{0,200}>")
_ENTITIES = {
    "&nbsp;": " ",
    "&rsquo;": "’",
    "&lsquo;": "‘",
    "&ldquo;": "“",
    "&rdquo;": "”",
    "&amp;": "&",
    "&quot;": '"',
    "&ndash;": "–",
    "&mdash;": "—",
    "&#39;": "'",
}


class FactbookParseError(SourceResponseError):
    pass


def _err(msg: str) -> FactbookParseError:
    return FactbookParseError(msg, retryable=False, kind="schema_drift")


def url_for(region: str, gec: str) -> str:
    return f"https://{HOST}/factbook/factbook.json/{COMMIT}/{region}/{gec}.json"


class FactbookConnector:
    def describe(self) -> ConnectorSpec:
        return common.law_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="CIA World Factbook (factbook/factbook.json mirror)",
            licence=RIGHTS["licence"],
            allowed_hosts=(HOST,),
            dataset_ids=(ATLAS_DATASET,),
            field_mappings={
                "commit": COMMIT,
                "data_commit": DATA_COMMIT,
                "fields": {k: list(v[0]) for k, v in TEXT_FIELDS.items()},
            },
            units={"text": "text", "suffrage.minimum_age": "years (age)"},
            geography=("ALL_FACTBOOK_ENTITIES",),
            time_semantics="as published on the last mirror data update (2026-01-22)",
            lineage_family="cia_world_factbook",
            cadence_seconds=365 * 86400,
            revision_strategy="pinned commit; source discontinued",
            max_response_bytes=MAX_BYTES,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=common.code_digest(sys.modules[__name__]),
        )

    def items(self, gecs: Sequence[str] | None = None) -> tuple[FetchItem, ...]:
        out = []
        for region, codes in PATHS.items():
            for gec in codes.split():
                if gecs is None or gec in gecs:
                    out.append(
                        FetchItem(
                            key=f"{region}/{gec}",
                            url=url_for(region, gec),
                            max_bytes=MAX_BYTES,
                            expected_media_types=("text/plain", "application/json"),
                        )
                    )
        return tuple(out)


def clean_text(s: str) -> str:
    """Factbook texts carry a few HTML entities/tags (e.g. ``&rsquo;``, ``<br>``): decoded/stripped."""
    t = _TAGS.sub(" ", s)
    for k, v in _ENTITIES.items():
        t = t.replace(k, v)
    return " ".join(t.split())


def _get(doc: Any, path: Sequence[str]) -> str | None:
    node = doc
    for k in path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    if isinstance(node, dict):
        node = node.get("text")
    return node if isinstance(node, str) and node.strip() else None


def families_mentioned(text: str) -> list[str]:
    low = text.lower()
    return [f for f, pat in FAMILY_KEYWORDS.items() if re.search(pat, low)]


def parse_profile(body: bytes, gec: str, region: str, raw_sha256: str) -> list[dict[str, Any]]:
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise _err(f"{region}/{gec}.json is not JSON") from e
    if not isinstance(doc, dict):
        raise _err(f"{region}/{gec}.json is not an object")
    geo = gec.upper()
    name = _get(doc, ("Government", "Country name", "conventional short form")) or _get(
        doc, ("Government", "Country name", "conventional long form")
    )
    base = {
        "transcription": common.VERBATIM,
        "licence": RIGHTS["licence"],
        "data_commit": DATA_COMMIT,
        "as_published": "factbook.json auto-update 2026-01-22 (last update before the CIA discontinued the Factbook)",
        "geo_code_system": "Factbook GEC code, mapped via FIPS 10-4",
    }
    if name:
        base["entity_name"] = clean_text(name)[:200]
    file = f"{region}/{gec}.json"
    rows: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    for indicator, (path, unit) in TEXT_FIELDS.items():
        raw = _get(doc, path)
        if raw is None:
            continue
        text, cut = common.clip_text(clean_text(raw), 3000)
        texts[indicator] = text
        flags = {**base, **({"value_text_truncated": True} if cut else {})}
        if text != raw.strip():
            flags["html_entities_decoded"] = True
        rows.append(
            common.row(
                source=ATLAS_SOURCE,
                dataset=ATLAS_DATASET,
                indicator=indicator,
                provider_geo=geo,
                period=AS_PUBLISHED,
                raw_sha256=raw_sha256,
                selector={"file": file, "commit": COMMIT, "json_path": list(path)},
                flags=flags,
                unit=unit,
                value_text=text,
            )
        )
    suff = texts.get("suffrage")
    if suff:
        m = _AGE.match(suff)
        if m:
            rows.append(
                common.row(
                    source=ATLAS_SOURCE,
                    dataset=ATLAS_DATASET,
                    indicator="suffrage.minimum_age",
                    provider_geo=geo,
                    period=AS_PUBLISHED,
                    raw_sha256=raw_sha256,
                    selector={"file": file, "commit": COMMIT, "json_path": ["Government", "Suffrage", "text"]},
                    flags={
                        **base,
                        "transcription": common.transcribed(f"Factbook {file} 'Suffrage' text"),
                        "rule": "leading '<N> years of age' of the suffrage text",
                        "source_text": suff[:500],
                    },
                    unit="years (age)",
                    value=str(int(m.group(1))),
                )
            )
    ls = texts.get("legal_system")
    if ls:
        fam = families_mentioned(ls)
        if fam:
            rows.append(
                common.row(
                    source=ATLAS_SOURCE,
                    dataset=ATLAS_DATASET,
                    indicator="legal_system.families_mentioned",
                    provider_geo=geo,
                    period=AS_PUBLISHED,
                    raw_sha256=raw_sha256,
                    selector={"file": file, "commit": COMMIT, "json_path": ["Government", "Legal system", "text"]},
                    flags={
                        **base,
                        "transcription": common.transcribed(f"Factbook {file} 'Legal system' text"),
                        "rule": "keyword mentions " + json.dumps(FAMILY_KEYWORDS),
                        "source_text": ls[:1500],
                        "caveat": "mentions of legal families in the text, not a classification",
                    },
                    unit="category list",
                    value_text="; ".join(fam),
                )
            )
    return rows


async def run(
    engine: Any,
    store: Any,
    http: Any,
    *,
    origin: str,
    gecs: Sequence[str] | None = None,
    actor: str = "worker:atlas_factbook",
) -> common.IngestReport:
    from reva.atlas.tables import register_atlas_table

    conn = FactbookConnector()
    spec = conn.describe()
    report = common.IngestReport(CONNECTOR_NAME)
    rows: list[dict[str, Any]] = []
    shas: list[str] = []
    for item in conn.items(gecs):
        got = await common.fetch_raw(http, item, SOURCE_KEY)
        if isinstance(got, common.Blocked):
            report.blocked.append(got)
            continue
        sha = common.archive_raw(engine, store, spec, got, actor=actor)
        shas.append(sha)
        region, gec = item.key.split("/")
        rows.extend(parse_profile(got.body, gec, region, sha))
    report.raw.append({"items": len(shas), "commit": COMMIT})
    if rows:
        s = register_atlas_table(
            engine,
            store,
            source=ATLAS_SOURCE,
            dataset=ATLAS_DATASET,
            frame=common.atlas_frame(rows),
            raw_sha256s=shas,
            parser_version=PARSER_VERSION,
            origin=origin,
            geo_scheme="fips",
            rights=RIGHTS,
            coverage_note=f"CIA World Factbook legal fields as of 2026-01-22 (mirror {DATA_COMMIT[:7]}); "
            f"{len(shas)} profiles; blocked: {len(report.blocked)}",
        )
        report.tables.append(
            {k: s[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created", "unmapped_provider_geo")}
            | {"countries": s["coverage"]["countries"]}
        )
    return report


__all__ = [
    "ATLAS_DATASET",
    "ATLAS_SOURCE",
    "CONNECTOR_NAME",
    "FactbookConnector",
    "families_mentioned",
    "parse_profile",
    "run",
]
