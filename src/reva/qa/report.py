"""QA report model and renderers (JSON + markdown)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Status = Literal["pass", "info", "warn", "fail", "error"]
REPORT_SCHEMA = "reva-qa-report/1"
_ORDER = {"fail": 0, "error": 1, "warn": 2, "info": 3, "pass": 4}


@dataclass
class CheckResult:
    """One check: ``count`` issues out of ``checked`` items; ``examples`` are a few issue rows (ids included)."""

    id: str
    domain: str
    title: str
    status: Status
    count: int = 0
    checked: int | None = None
    examples: list[dict[str, Any]] = field(default_factory=list)
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], {})}


def status_for(count: int, severity: Status) -> Status:
    return "pass" if count == 0 else severity


@dataclass
class QaReport:
    generated_at: str
    as_of: str
    origin: str
    mode: str
    config_sha256: str
    domains: list[str]
    checks: list[CheckResult] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, int]:
        out = {k: 0 for k in _ORDER}
        for c in self.checks:
            out[c.status] += 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "generated_at": self.generated_at,
            "as_of": self.as_of,
            "origin": self.origin,
            "mode": self.mode,
            "config_sha256": self.config_sha256,
            "domains": self.domains,
            "summary": self.summary(),
            "context": self.context,
            "checks": [c.as_dict() for c in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, default=str, ensure_ascii=False)

    def to_markdown(self, *, max_examples: int = 5, max_breakdown: int = 12) -> str:
        s = self.summary()
        lines = [
            "# REVA data-quality report (`reva qa run`)",
            "",
            f"Generated {self.generated_at} · as of {self.as_of} · origin `{self.origin}` · mode `{self.mode}` · "
            f"config `registry/qa/checks.yaml` sha256 `{self.config_sha256[:12]}…`",
            "",
            "Read-only: every finding is reported, nothing is fixed, retracted or rewritten. A cross-source divergence "
            "is a difference between two publishers to review, never a reason to blend or pick a value.",
            "",
            "Summary: " + ", ".join(f"{k} {v}" for k, v in s.items() if v),
            "",
        ]
        if self.context:
            lines += ["Context: " + ", ".join(f"{k} {v}" for k, v in self.context.items()), ""]
        lines += ["| domain | check | status | issues | checked |", "|---|---|---|---:|---:|"]
        for c in self.checks:
            checked = "" if c.checked is None else f"{c.checked:,}"
            lines.append(f"| {c.domain} | `{c.id}` {c.title} | **{c.status}** | {c.count:,} | {checked} |")
        lines.append("")
        for dom in self.domains:
            cs = [c for c in self.checks if c.domain == dom]
            if not cs:
                continue
            lines += [f"## {dom}", ""]
            for c in sorted(cs, key=lambda c: (_ORDER[c.status], c.id)):
                lines.append(f"### `{c.id}` — {c.title} ({c.status}, {c.count:,} issues)")
                if c.note:
                    lines += ["", c.note]
                if c.breakdown:
                    lines += ["", _table(c.breakdown[:max_breakdown])]
                    if len(c.breakdown) > max_breakdown:
                        lines.append(f"\n… {len(c.breakdown) - max_breakdown} more rows in the JSON report.")
                if c.examples:
                    lines += ["", "Examples:", ""]
                    lines += [f"* `{_short(e)}`" for e in c.examples[:max_examples]]
                lines.append("")
        return "\n".join(lines) + "\n"


def _short(e: dict[str, Any], n: int = 400) -> str:
    t = json.dumps(e, default=str, ensure_ascii=False)
    return t if len(t) <= n else t[: n - 1] + "…"


def _table(rows: list[dict[str, Any]]) -> str:
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        cells = []
        for k in cols:
            v = r.get(k, "")
            v = f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else v
            v = f"{v:.4g}" if isinstance(v, float) else v
            cells.append(str(v).replace("|", "\\|")[:120])
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)
