"""SSA "Social Security Programs Throughout the World" (SSPTW) — access probe only (V3, agent A-LAW).

Intended content: statutory retirement ages (men/women), contribution rates and program types for
~180 countries from the SSA/ISSA regional editions (Europe, Asia and the Pacific, Africa, the
Americas), with page/table locators.

Status (checked 2026-10-15): ``www.ssa.gov`` answers **HTTP 403** (Akamai "Access Denied") to this
deployment's egress for every path tried (the SSPTW index, the site root, a regional PDF), with a
browser-like User-Agent too; ``socialsecurity.gov`` redirects there; the Internet Archive copy is
blocked by the environment's egress policy. No SSPTW byte could be read, so:

* no parser is shipped — writing one against a format nobody here could inspect would be guessing;
* :func:`run` makes the one probe request and returns a typed :class:`~.atlas_law_common.Blocked`
  (``http_403`` …). If access works one day, it archives the index page and still returns a typed
  ``Blocked`` with reason ``parser_not_implemented`` — never an empty "success";
* the open replacement for statutory retirement ages of all economies is the World Bank WBL 2026
  Pension topic (``wbl.wbl2026`` indicators ``Pen_Law_PensAgeEq_FullM/FullW/PartM/PartW`` and
  ``Pen_Law_MandAgeEq_Men/Women``, each with WBL's legal-basis citation), see
  ``registry/atlas/catalog/law.yaml`` and ``docs/dev/v3_a_law.md``.
"""

from __future__ import annotations

import sys
from typing import Any

from reva.adapters.connectors import atlas_law_common as common
from reva.ports.connectors import ConnectorSpec, FetchItem

SOURCE_KEY = "ssa.ssptw"
CONNECTOR_NAME = "atlas.ssa_ssptw"
CONNECTOR_VERSION = "0.1.0"
PARSER_VERSION = "atlas-ssa-ssptw-probe-1"
HOST = "www.ssa.gov"
INDEX_URL = f"https://{HOST}/policy/docs/progdesc/ssptw/"
ACCESS_CHECK = {
    "checked_at": "2026-10-15",
    "result": "HTTP 403 Access Denied (Akamai) for https://www.ssa.gov/, the SSPTW index and a regional PDF",
    "alternatives_tried": [
        "socialsecurity.gov (redirects to www.ssa.gov)",
        "web.archive.org (blocked by the environment egress policy)",
    ],
    "open_replacement": "wbl.wbl2026 Pen_Law_PensAgeEq_* (World Bank WBL 2026, CC BY 4.0, legal basis per cell)",
}


class SsaSsptwConnector:
    def describe(self) -> ConnectorSpec:
        return common.law_spec(
            name=CONNECTOR_NAME,
            version=CONNECTOR_VERSION,
            source_key=SOURCE_KEY,
            provider="US Social Security Administration / ISSA",
            licence="US Government work (public domain in the US)",
            allowed_hosts=(HOST,),
            dataset_ids=("ssptw",),
            field_mappings={"status": "probe_only", "access_check": ACCESS_CHECK},
            units={},
            geography=("ALL_SSPTW_COUNTRIES",),
            time_semantics="edition year (rules in force at the edition's reference date)",
            lineage_family="ssa_issa_ssptw",
            cadence_seconds=365 * 86400,
            revision_strategy="biennial regional editions",
            max_response_bytes=2_000_000,
            max_pages=1,
            parser_version=PARSER_VERSION,
            code_digest=common.code_digest(sys.modules[__name__]),
        )

    def items(self) -> tuple[FetchItem, ...]:
        return (FetchItem(key="index", url=INDEX_URL, expected_media_types=("text/html",), max_bytes=2_000_000),)


async def run(
    engine: Any, store: Any, http: Any, *, origin: str, actor: str = "worker:atlas_ssa"
) -> common.IngestReport:
    """One probe request. Always ends with a typed ``Blocked`` entry and no atlas table."""
    conn = SsaSsptwConnector()
    report = common.IngestReport(CONNECTOR_NAME)
    (item,) = conn.items()
    got = await common.fetch_raw(http, item, SOURCE_KEY)
    if isinstance(got, common.Blocked):
        report.blocked.append(got)
        return report
    sha = common.archive_raw(engine, store, conn.describe(), got, actor=actor)
    report.raw.append({"item": item.key, "raw_sha256": sha, "bytes": len(got.body), "url": item.url})
    report.blocked.append(
        common.Blocked(
            SOURCE_KEY,
            item.key,
            "parser_not_implemented",
            "SSA answered; the SSPTW layout was never inspectable from this deployment, so no extraction "
            "exists yet (index page archived for the parser work)",
        )
    )
    return report


__all__ = ["ACCESS_CHECK", "CONNECTOR_NAME", "SsaSsptwConnector", "run"]
