"""SSA "Social Security Programs Throughout the World" (SSPTW) — access probe only (V3, agent A-LAW).

Intended content: statutory retirement ages (men/women), contribution rates and program types for
~180 countries from the SSA/ISSA regional editions (Europe, Asia and the Pacific, Africa, the
Americas), with page/table locators.

Status (checked 2026-10-15): ``www.ssa.gov`` answers **HTTP 403** (Akamai "Access Denied") to this
deployment's egress for every path tried (the SSPTW index, the site root, a regional PDF), with a
browser-like User-Agent too; ``socialsecurity.gov`` redirects there; the Internet Archive copy is
blocked by the environment's egress policy. No SSPTW byte could be read, so:

* no parser is shipped — writing one against a format nobody here could inspect would be guessing;
* :func:`atlas_ingest` makes the one probe request and returns a typed :class:`~.atlas_law_common.Blocked`
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
from reva.atlas.sources import AtlasSourceSpec

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


def atlas_source() -> AtlasSourceSpec:
    """The ATLASCORE source spec (connector ``atlas.ssa_ssptw``; budget key ``ssa.ssptw``): probe only."""
    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source="ssa",
        provider="US Social Security Administration / ISSA",
        licence="US Government work (public domain in the US)",
        allowed_hosts=(HOST,),
        datasets=("ssptw",),
        dataset_ids=("ssptw",),
        cadence_seconds=365 * 86400,
        planned_requests=1,
        max_response_bytes=2_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=common.code_digest(sys.modules[__name__]),
        field_mappings={"status": "probe_only", "access_check": ACCESS_CHECK},
        notes="Access probe only: no parser (SSPTW bytes were never readable from this deployment).",
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """One probe request. Always ends with a typed ``Blocked`` entry and no atlas table."""
    report = common.IngestReport(CONNECTOR_NAME)
    if not run.wants("ssptw"):
        return report.as_dict()
    got = await common.fetch(run, INDEX_URL, key="index", source_key=SOURCE_KEY, media_types=("text/html",))
    if isinstance(got, common.Blocked):
        report.blocked.append(got)
        return report.as_dict()
    sha = run.archive(got)
    report.raw.append({"item": "index", "raw_sha256": sha, "bytes": len(got.body), "url": INDEX_URL})
    report.blocked.append(
        common.Blocked(
            SOURCE_KEY,
            "index",
            "parser_not_implemented",
            "SSA answered; the SSPTW layout was never inspectable from this deployment, so no extraction "
            "exists yet (index page archived for the parser work)",
        )
    )
    return report.as_dict()


__all__ = ["ACCESS_CHECK", "CONNECTOR_NAME", "atlas_ingest", "atlas_source"]
