"""Load ``registry/qa/checks.yaml`` (tolerances and declared rules of the QA checks)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from reva.kernel.errors import ValidationProblem

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKS_FILE = REPO_ROOT / "registry" / "qa" / "checks.yaml"
CONFIG_SCHEMA = "reva-qa-checks/1"
DOMAINS = ("cross_source", "units", "time", "graph", "licence", "provenance", "causal")


@dataclass(frozen=True)
class QaConfig:
    raw: dict[str, Any]
    sha256: str
    path: str

    def section(self, name: str) -> dict[str, Any]:
        v = self.raw.get(name) or {}
        if not isinstance(v, dict):
            raise ValidationProblem("qa config section must be a mapping", section=name)
        return v

    def tolerance(self, name: str) -> dict[str, Any]:
        """Cross-source tolerances for a concept id / pair name: defaults, then the first matching override."""
        cs = self.section("cross_source")
        tol = dict(cs.get("defaults") or {})
        for o in cs.get("overrides") or []:
            if re.search(o["match"], name):
                tol.update({k: v for k, v in o.items() if k != "match"})
                break
        return tol


def load_config(path: Path = CHECKS_FILE) -> QaConfig:
    data = path.read_bytes()
    doc = yaml.safe_load(data)
    if not isinstance(doc, dict) or doc.get("schema") != CONFIG_SCHEMA:
        raise ValidationProblem("qa config must declare schema " + CONFIG_SCHEMA, path=str(path))
    for o in (doc.get("cross_source") or {}).get("overrides") or []:
        re.compile(o["match"])
    for b in (doc.get("units") or {}).get("bounded") or []:
        re.compile(b["unit_regex"])
        if not isinstance(b.get("min"), (int, float)) or not isinstance(b.get("max"), (int, float)):
            raise ValidationProblem("bounded rule needs numeric min and max", rule=b.get("name"))
    return QaConfig(doc, hashlib.sha256(data).hexdigest(), str(path))
