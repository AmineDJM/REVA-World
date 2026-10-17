"""Curated published estimates (``registry/effects/literature/<entry>.yaml``) -> :class:`EffectEstimateIn`.

One file = one publication (the version actually read) = one loader ``dataset``. Every number is quoted from the
source with its location (``locator``, ``pdf_page``) and a verbatim ``quote`` containing the number as printed.
Transcriptions are typed by an agent (``transcribed``); :func:`text_check` re-finds every quote automatically in
the text extracted from the archived bytes (``pdftotext -layout``) and only then marks the estimate
``transcribed_double_checked``. A quote that is not found, or found on another page than ``pdf_page``, rejects
the estimate (never silently loaded). Nothing here is invented: an entry without a readable number is not
written.
"""

from __future__ import annotations

import copy
import hashlib
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from reva.effects.model import EffectEstimateIn, Horizon, IrfPoint, Uncertainty, normalise_text
from reva.kernel.errors import ValidationProblem
from reva.ontology.registry import REGISTRY_ROOT

LITERATURE_DIR = REGISTRY_ROOT / "effects" / "literature"
SCHEMA = "reva-effects-literature/1"
SOURCE_KEY = "effects.literature"
PDFTOTEXT_TIMEOUT_S = 120
_ENTRY_KEYS = {
    "schema",
    "id",
    "source_key",
    "citation",
    "document",
    "as_of",
    "rights",
    "curation",
    "defaults",
    "estimates",
    "notes",
}
_EST_KEYS = {
    "id",
    "cause",
    "effect",
    "measure",
    "unit",
    "shock",
    "sign",
    "point",
    "point_kind",
    "as_printed",
    "uncertainty",
    "horizon",
    "irf",
    "scope",
    "method",
    "identification",
    "meta",
    "locator",
    "pdf_page",
    "quote",
    "applicability",
    "source_caveats",
    "printed_transform",
    "evidence",
    "causal_claim_keys",
}


@dataclass
class Entry:
    id: str
    source_key: str
    citation: dict[str, Any]
    document: dict[str, Any]
    as_of: str
    rights: dict[str, Any]
    curation: dict[str, Any]
    estimates: list[dict[str, Any]]
    path: Path | None = None
    sha256: str = ""
    notes: str | None = None

    def pdf_pages(self) -> dict[str, int | None]:
        return {e["id"]: e.get("pdf_page") for e in self.estimates}


@dataclass
class TextCheck:
    tool: str | None
    pages: int = 0
    found: dict[str, int | None] = field(default_factory=dict)  # estimate id -> 1-based page (None = not found)
    problems: dict[str, str] = field(default_factory=dict)  # estimate id -> reason

    def as_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "pages": self.pages, "found": dict(self.found), "problems": dict(self.problems)}


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dict(base))
    for k, val in over.items():
        if isinstance(val, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = _merge(out[k], val)
        else:
            out[k] = copy.deepcopy(val)
    return out


def parse_entry(data: bytes, path: Path | None = None) -> Entry:
    doc = yaml.safe_load(data)
    name = str(path or "<bytes>")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValidationProblem(f"{name}: schema must be {SCHEMA}")
    bad = set(doc) - _ENTRY_KEYS
    if bad:
        raise ValidationProblem(f"{name}: unknown keys", keys=sorted(bad))
    for k in ("id", "citation", "document", "as_of", "rights", "curation", "estimates"):
        if not doc.get(k):
            raise ValidationProblem(f"{name}: '{k}' is required")
    if path is not None and path.stem != doc["id"]:
        raise ValidationProblem(f"{name}: file name must be <id>.yaml")
    d = doc["document"]
    if not isinstance(d, dict) or not d.get("url") or not d.get("media_type"):
        raise ValidationProblem(f"{name}: document.url and document.media_type are required")
    defaults = doc.get("defaults") or {}
    ests: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, e in enumerate(doc["estimates"]):
        if not isinstance(e, dict) or not e.get("id"):
            raise ValidationProblem(f"{name}: estimate {i} needs an id")
        merged = _merge(defaults, e)
        bad = set(merged) - _EST_KEYS
        if bad:
            raise ValidationProblem(f"{name}: estimate {e['id']} has unknown keys", keys=sorted(bad))
        if merged["id"] in seen:
            raise ValidationProblem(f"{name}: duplicate estimate id {merged['id']}")
        seen.add(merged["id"])
        ests.append(merged)
    return Entry(
        id=str(doc["id"]),
        source_key=str(doc.get("source_key") or SOURCE_KEY),
        citation=dict(doc["citation"]),
        document=dict(d),
        as_of=str(doc["as_of"]),
        rights=dict(doc["rights"]),
        curation=dict(doc["curation"]),
        estimates=ests,
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        notes=doc.get("notes"),
    )


def load_entries(directory: Path = LITERATURE_DIR) -> list[Entry]:
    return [parse_entry(p.read_bytes(), p) for p in sorted(directory.glob("*.yaml"))]


# ---------------------------------------------------------------------------------------------- text check
def pdftotext_available() -> bool:
    return shutil.which("pdftotext") is not None


def pdf_pages_text(body: bytes) -> list[str]:
    """Text of each page (``pdftotext -layout``, form-feed separated). The bytes are passed on stdin."""
    exe = shutil.which("pdftotext")
    if exe is None:
        raise RuntimeError("pdftotext is not installed")
    out = subprocess.run(  # noqa: S603 - fixed binary, bytes on stdin, no shell
        [exe, "-layout", "-enc", "UTF-8", "-", "-"],
        input=body,
        capture_output=True,
        timeout=PDFTOTEXT_TIMEOUT_S,
        check=True,
    ).stdout.decode("utf-8", "replace")
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages


def text_check(entry: Entry, body: bytes) -> TextCheck:
    """Find every estimate's quote in the archived document (1-based page) and compare with ``pdf_page``."""
    if entry.document.get("media_type") != "application/pdf":
        pages = [body.decode("utf-8", "replace")]
        tool = "utf-8 text"
    elif not pdftotext_available():
        return TextCheck(tool=None)
    else:
        pages = pdf_pages_text(body)
        tool = "pdftotext -layout"
    norm = [normalise_text(p) for p in pages]
    tc = TextCheck(tool=tool, pages=len(pages))
    for e in entry.estimates:
        q = e.get("quote")
        if not q:
            continue
        nq = normalise_text(q)
        hits = [i + 1 for i, p in enumerate(norm) if nq in p]
        page = hits[0] if hits else None
        tc.found[e["id"]] = page
        if page is None:
            tc.problems[e["id"]] = "quote_not_in_source"
        elif e.get("pdf_page") is not None and int(e["pdf_page"]) not in hits:
            tc.problems[e["id"]] = "locator_page_mismatch"
    return tc


# ---------------------------------------------------------------------------------------------- estimates
def _unc(u: Mapping[str, Any] | None) -> Uncertainty:
    u = dict(u or {})
    return Uncertainty(
        se=u.get("se"),
        ci_low=u.get("ci_low"),
        ci_high=u.get("ci_high"),
        ci_level=u.get("ci_level"),
        interval_kind=u.get("interval_kind"),
        draws=u.get("draws"),
        status=u.get("status", "not_reported"),
        se_as_printed=u.get("se_as_printed"),
    )


def to_estimates(
    entry: Entry, *, raw_sha256: str, check: TextCheck | None = None
) -> tuple[list[EffectEstimateIn], dict[str, str]]:
    """Estimates of an entry (``raw_sha256`` = the archived document) and the ids refused by the text check."""
    out: list[EffectEstimateIn] = []
    refused: dict[str, str] = {}
    for e in entry.estimates:
        if check is not None and e["id"] in check.problems:
            refused[e["id"]] = check.problems[e["id"]]
            continue
        checked = check is not None and check.tool is not None and check.found.get(e["id"]) is not None
        shock = e.get("shock") or {}
        h = e.get("horizon") or {}
        page = check.found.get(e["id"]) if check is not None else None
        curation = {
            **entry.curation,
            "entry_sha256": entry.sha256,
            "evidence": e.get("evidence", "primary"),
            "printed_transform": e.get("printed_transform"),
            "text_check": (
                {"tool": check.tool, "page": page} if check is not None and check.tool else {"tool": None}
            ),
            "pdf_page": e.get("pdf_page"),
            "document_version_read": entry.document.get("version_read"),
        }
        out.append(
            EffectEstimateIn(
                estimate_id=str(e["id"]),
                entry_id=entry.id,
                cause_kind=e["cause"]["kind"],
                cause_id=e["cause"]["id"],
                cause_definition=e["cause"]["definition"],
                cause_transform=e["cause"]["transform"],
                effect_kind=e["effect"]["kind"],
                effect_id=e["effect"]["id"],
                effect_definition=e["effect"]["definition"],
                effect_transform=e["effect"]["transform"],
                measure=e["measure"],
                unit=e["unit"],
                shock_size=shock.get("size"),
                shock_unit=shock.get("unit"),
                sign=e["sign"],
                point=e.get("point"),
                point_kind=e.get("point_kind", "point" if e.get("point") is not None else None),
                as_printed=e.get("as_printed"),
                uncertainty=_unc(e.get("uncertainty")),
                horizon=Horizon(h.get("kind", "unspecified"), h.get("value"), h.get("unit")),
                irf=tuple(IrfPoint(**p) for p in e.get("irf") or ()),
                scope=e.get("scope") or {},
                method=e["method"],
                identification=e.get("identification"),
                meta=e.get("meta"),
                citation=entry.citation,
                locator=e.get("locator"),
                quote=e.get("quote"),
                transcription="transcribed_double_checked" if checked else "transcribed",
                raw_sha256=raw_sha256,
                as_of=entry.as_of,
                applicability=e.get("applicability"),
                source_caveats=e.get("source_caveats"),
                causal_claim_keys=tuple(e.get("causal_claim_keys") or ()),
                rights=entry.rights,
                curation=curation,
            )
        )
    return out, refused


def document_urls(entries: Sequence[Entry]) -> list[str]:
    return sorted({e.document["url"] for e in entries})
