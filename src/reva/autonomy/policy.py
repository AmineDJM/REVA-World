"""Versioned autonomy policies, kill switch, budgets and backlog bounds.

The autonomy agent can read policies but never edit them: every policy file it
relies on is verified against a digest pinned in this reviewed module (T3 code,
changed only by maintainers). If a policy file's bytes change, gates refuse to
run until a maintainer re-pins the digest. Text fetched from providers is data
and has no path into this module: allowlists, tiers and scopes come only from
the pinned policy bytes.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection, text

from reva.autonomy.common import AutonomyBlocked
from reva.ontology.registry import REGISTRY_ROOT
from reva.ports.autonomy import ArtifactRef

POLICY_DIR = REGISTRY_ROOT / "policies"

# Maintainer-pinned SHA-256 of policy file bytes. Changing a policy requires a
# reviewed change to these pins (T3); the agent has no write path to either.
# History of deliberate re-pins:
# * promotion.yaml -> promotion-1.1.0 (2026-10-15, R4 fixes): pins the reviewed
#   eurostat_jsonstat template code digest (approved_templates.*.template_sha256,
#   checked by the gate), names the template's registered source (source_key), and
#   adds explicit model_promotion test settings. Any later edit of
#   src/reva/adapters/connectors/eurostat_template.py changes the template digest
#   and disables T1 auto-promotion until a maintainer re-pins both digests.
# * promotion.yaml re-pinned (2026-10-15, R6 fixes, label stays promotion-1.1.0):
#   template_sha256 covers the template's whole reva import closure + connector schema
#   (R6-07); rights_ref pinned per template (R6-12); approved_exact_mappings entries pin
#   the provider structure (dimension order, unit code, category vocabularies, totals) (R6-04).
# * budgets.yaml -> budgets-1.1.0 (2026-10-15, V2 step 2, agent NLC): adds the bounded
#   llm.scenario_drafts budget (daily requests/tokens, max output tokens) for natural-language
#   scenario drafts (src/reva/nl); llm.monthly_usd for autonomy extraction stays 0. Egress
#   limits unchanged.
# * budgets.yaml -> budgets-1.2.0 (2026-10-15, V2 step 3a, agent POP): adds the per-source
#   daily request limit for the built-in connector eurostat.demography (300; a full run is
#   282 small time-window requests). No other limit changed.
# * budgets.yaml -> budgets-1.3.x (2026-10-15, V3, agent ATLASCORE, the only V3 editor of this
#   pin): per-source daily limits for registered V3 atlas/KG sources merged from
#   registry/atlas/budget_requests/*.yaml by src/reva/atlas/budget_merge.py (max request per
#   source, <= 5000/day; pre-V3 limits never lowered); daily totals = 1.2.0 base + merged keys
#   (requests) + declared bytes/day, capped at 20000 requests / 3 GiB. LLM budgets unchanged.
#   Each later merge bumps the patch version and re-pins here.
# * budgets.yaml -> budgets-1.3.3 (2026-10-15, V3, agent ATLASOPS, successor of ATLASCORE for
#   atlas integration): merge of the pending requests for newly registered sources
#   (factbook.json, fao.faostat, unsd.ama, worldbank.atlas); same caps, totals capped at 3 GiB.
# * budgets.yaml -> budgets-1.3.4 (2026-10-15, ATLASOPS): merge of ssa.ssptw (A-LAW, 220/day) and of
#   the KGGEOPOL sources cshapes.cran, harvard.dataverse.unvotes, ucdp.downloads (kggeopol.yaml).
# * budgets.yaml -> budgets-1.3.5 (2026-10-16, ATLASOPS): merge of the requests filed by finished agents
#   since 1.3.4 (A-SOCIETY, A-RESOURCES, KGGEOPOL, A-FACTBOOK, ...); same caps (<= 5000/source, 3 GiB/day).
# * budgets.yaml -> budgets-1.3.6 (2026-10-16, ATLASOPS): merge of requests filed since 1.3.5 (A-MILITARY un.psdata,
#   A-FISCAL bis.stats + oecd.sdmx raise, KGLANG glottolog/wals/grambank, ...); same caps.
# * budgets.yaml -> budgets-1.3.7 (2026-10-16, lead): merge of the causality-wave requests (CAUSDATA icb.data,
#   A-ECRISES, A-COUPS, A-SANCTIONS, A-CONTENTION, A-HAZARDS, CAUSCAND, ...); same caps (<= 5000/source, 3 GiB/day).
# * budgets.yaml -> budgets-1.3.8 (2026-10-16, lead): merge of A-COLONIAL (coldat, icow, cow terrchange); same caps.
# * budgets.yaml -> budgets-1.3.9 (2026-10-17, CAUSMORE): merge of causmore.yaml (cow.data 34 -> 44 for
#   atlas.cowwars; new pax.peaceagreements 6, idmc.gidd 6); same caps.
# * budgets.yaml -> budgets-1.3.10 (2026-10-17, SAMEAS): merge of sameas.yaml (pax.peaceagreements 6 -> 12: same-day
#   re-run of atlas.pax after its 1.0.0 -> 1.1.0 change); same caps.
# * budgets.yaml -> budgets-1.3.11 (2026-10-17, AUDFIX): merge of audfix.yaml (archived codebooks of the audit fixes,
#   same-day re-runs: harvard.dataverse.navco 20 -> 40, .mec 6 -> 12, .massmob 6 -> 12, archigos.leaders 18 -> 30,
#   esrb.fcdb 3 -> 8, ties.unc 8 -> 16); same caps.
# * budgets.yaml -> budgets-1.3.12 (2026-10-17, AUDFIX): noaa.ncei_hazards 600 -> 800 (corrective same-day re-run of
#   atlas.noaa_hazards after a registry label-quoting fix); same caps.
# * budgets.yaml -> budgets-1.3.13 (2026-10-17, FIXB): merge of fixb.yaml (same-day re-runs of the audit2 dataset
#   fixes: idmc.gidd 6 -> 12, ucdp.downloads 30 -> 40; cow.data unchanged at 44); same caps.
# * budgets.yaml -> budgets-1.3.14 (2026-10-17, V4LINK): merge of v4link.yaml (new unsd.classifications 30/day for
#   atlas.unsd_classif) and of the V4 requests filed since 1.3.13 (v4energy: worldbank.cmo, wri.gppd); same caps.
# * budgets.yaml -> budgets-1.3.15 (2026-10-17, V4MIND): merge of v4mind.yaml (new hofstede.dimensions 4/day,
#   openalex.snapshot 30/day; oecd.sdmx 76 -> 82, who.gho 129 -> 160, ilostat.rplumber 60 -> 75, openalex.api 400 -> 700).
# * budgets.yaml -> budgets-1.3.16 (2026-10-17, V4HEALTH): merge of v4health.yaml (new uberon.obo 8/day, doid.obo 8/day,
#   mondo.obo 8/day, clingen.gene_validity 4/day, who.ghe 400/day); same caps.
# * budgets.yaml -> budgets-1.3.17 (2026-10-17, V4ECON): merge of v4econ.yaml (new fao.ffpi 4/day; imf.sdmx 60 -> 76,
#   oecd.sdmx 82 -> 92, eurostat.dissemination 100 -> 116, worldbank.atlas 40 -> 42); same caps.
# * budgets.yaml -> budgets-1.3.18 (2026-10-17, V4INDUSTRY): merge of v4industry.yaml (new gleif.api 3000/day;
#   oecd.sdmx +15) and of the V4 requests filed since 1.3.17 (chebi.release, ...); same caps.
# * budgets.yaml -> budgets-1.3.21 (2026-10-17, re-pinned by V4INDUSTRY): the file committed by a WIP snapshot (merges
#   of later V4 requests, e.g. V4ECO fao.fishstat) had not been re-pinned; content unchanged by budget_merge.
# * budgets.yaml -> budgets-1.3.19 (2026-10-17, V4CLIMATE): merge of v4climate.yaml (new climate.global_indices 80/day,
#   noaa.ghcnm 10/day, carbonbrief.attribution 6/day, naturalearth.admin1_10m 2/day; worldbank.cckp 6 -> 80).
# * budgets.yaml -> budgets-1.3.20 (2026-10-17, V4ECO): merge of v4eco.yaml (new gbif.occurrence 600/day,
#   globi.interactions 200/day, resolve.ecoregions2017 6/day, fao.fishstat 4/day); same caps.
# * budgets.yaml -> budgets-1.3.21 (2026-10-17, PRED-VINTAGE): merge of pred_vintage.yaml (new dbnomics.api 80/day,
#   philfed.data 20/day, ecb.data_api 6/day; oecd.sdmx 92 -> 120) and of pred_effect.yaml filed since 1.3.20
#   (effects.literature 40/day); same caps.
# * budgets.yaml -> budgets-1.3.23 (2026-10-17, PRED-HF): merge of pred_hf.yaml (new daily-series sources ecb.stats
#   12/day, bis.bulk 6/day, ustreasury.rates 130/day, eia.dnav 32/day, boe.iadb 8/day, nyfed.markets 12/day); same caps.
# * budgets.yaml -> budgets-1.3.24 (2026-10-17, V4LINK): new qlever.wikidata 60/day (atlas.v4link_qlever, the
#   Wikidata identifier bridges read from QLever while WDQS throttles the shared egress); same caps.
# * budgets.yaml -> budgets-1.3.26 (2026-10-17, PRED-ENERGY): merge of pred_energy.yaml (new eia.steo_archive 70/day,
#   STEO archive vintages) and of the pending v6signals.yaml (gdelt.doc2 100/day, gdelt.events2 1600/day); same caps.
# * budgets.yaml -> budgets-1.3.27 (2026-10-17, PRED-CONFLICT): new views.api 60/day (atlas.views, VIEWS conflict
#   forecasts as an attributed benchmark); same caps.
# * budgets.yaml -> budgets-1.3.28 (2026-10-17, V6SIGNALS): gdelt.doc2 100 -> 300/day (DOC 2.0 API requests refused with
#   HTTP 429 are retried after a cooldown, worst case 222 per run); same caps.
# * budgets.yaml -> budgets-1.3.29 (2026-10-17, merge run by V6CULTURE, pinned by V6SIGNALS): 1.3.28 plus v6culture.yaml
#   (new musicbrainz.stats 4/day, openlibrary.api 500/day); same caps.
# * budgets.yaml -> budgets-1.3.30 (2026-10-17, merge run and pinned by V6INFRA): 1.3.29 plus v6infra.yaml (new
#   unece.unlocode 10/day, imf.portwatch 80/day; nga.wpi 3 -> 6, ourairports.data 6 -> 10, eurostat.dissemination
#   116 -> 140) and the requests pending at merge time (pred_fin, pred_health, v6drugs, v6conflict); same caps.
# * budgets.yaml -> budgets-1.3.31 (2026-10-17, merge run and pinned by V6FINDATA): 1.3.30 plus v6findata.yaml (new
#   frb.releases 8/day, kfrench.library 12/day; bis.stats 14 -> 24, bis.bulk 6 -> 14, ecb.stats 12 -> 28,
#   boe.iadb 8 -> 16, imf.sdmx 76 -> 80) and the requests pending at merge time; same caps.
# * budgets.yaml -> budgets-1.3.32 (2026-10-17, merge run and pinned by EFFECTS-MORE): 1.3.31 plus effects_more.yaml
#   (effects.literature 40 -> 100/day: one request per curated publication, 34 after V6); same caps.
# * budgets.yaml -> budgets-1.3.33 (2026-10-17, merge run and pinned by V6MIGR): 1.3.32 plus v6migr.yaml (new
#   unhcr.asylum 10/day, iom.missing_migrants 8/day); same caps.
# * budgets.yaml -> budgets-1.3.34 (2026-10-17, merge run and pinned by V6POLITICS): 1.3.33 plus v6politics.yaml (new
#   nelda.dataverse 10/day, oxcgrt.github 8/day, whogov.dataverseno 12/day; parlgov.dataverse 6 -> 10); same caps.
# * budgets.yaml -> budgets-1.3.35 (2026-10-17, merge run and pinned by V6AGRI): 1.3.34 plus v6agri.yaml (new
#   usda.psd 6/day, usda.wasde 100/day, fao.faostat_tm 4/day, ifpri.export_restrictions 6/day); same caps.
# * budgets.yaml -> budgets-1.3.36 (2026-10-17, merge run and pinned by V6DIGITAL): 1.3.35 plus v6digital.yaml (new
#   accessnow.stop 8/day, cfr.cyber_operations 6/day); same caps.
# * budgets.yaml -> budgets-1.3.38 (2026-10-17, merge run and pinned by V6TRADEHF; 1.3.37 was an intermediate
#   write of the same merge): 1.3.36 plus v6tradehf.yaml (new eurostat.comext 300/day; imf.sdmx 80 -> 98); same caps.
# * budgets.yaml -> budgets-1.3.39 (2026-10-17, merge run and pinned by V6DRUGS): 1.3.38 plus v6drugs.yaml update
#   (who.eeml 2000 -> 4000/day: a re-run after a failed run must fit the same day); same caps.
# * budgets.yaml -> budgets-1.3.40 (2026-10-17, merge run and pinned by BEAT-AGRI): 1.3.39 plus beat_agri.yaml (new
#   usda.wasde_esmis 450/day, noaa.ncei_cag 12/day); same caps.
# * budgets.yaml -> budgets-1.3.41 (2026-10-17, merge run and pinned by BEAT-NOWCAST): 1.3.40 plus beat_nowcast.yaml
#   (new fed.nowcasts 30/day); same caps.
PINNED_POLICY_DIGESTS: dict[str, str] = {
    "promotion.yaml": "115a9ab5aed0c29e136fbd8efa9d519e5db5db6eaa241719b9a0b9d7384194aa",
    "budgets.yaml": "95c139e128e3ff30544fab784406eedd1dc5a4e21d6c2754ab6c59dba2c6da4a",
}


class PolicyIntegrityError(AutonomyBlocked):
    code = "policy_integrity"


@dataclass(frozen=True)
class PinnedPolicy:
    path: Path
    sha256: str
    content: Mapping[str, Any]

    @property
    def version(self) -> str:
        return str(self.content.get("policy_version", "unversioned"))

    def ref(self) -> ArtifactRef:
        return ArtifactRef(sha256=self.sha256, namespace="policy", media_type="application/yaml")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_pinned(path: Path, expected_sha256: str) -> PinnedPolicy:
    """Load a policy only if its bytes match the pinned digest."""
    data = path.read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if got != expected_sha256:
        raise PolicyIntegrityError(
            f"policy {path.name} changed (sha256 {got[:12]}… != pinned {expected_sha256[:12]}…); "
            "gates refuse until a maintainer re-pins it",
            policy=path.name,
            expected=expected_sha256,
            actual=got,
        )
    content = yaml.safe_load(data)
    if not isinstance(content, dict):
        raise PolicyIntegrityError(f"policy {path.name} is not a mapping")
    return PinnedPolicy(path, got, content)


def promotion_policy(path: Path | None = None, expected: str | None = None) -> PinnedPolicy:
    return load_pinned(path or POLICY_DIR / "promotion.yaml", expected or PINNED_POLICY_DIGESTS["promotion.yaml"])


def budgets_policy(path: Path | None = None, expected: str | None = None) -> PinnedPolicy:
    return load_pinned(path or POLICY_DIR / "budgets.yaml", expected or PINNED_POLICY_DIGESTS["budgets.yaml"])


@dataclass
class AutonomyGuard:
    """Kill switch, mode and budget checks consulted before every autonomy action."""

    kill_switch: bool = False
    mode: str = "template_only"
    llm_enabled: bool = False
    llm_monthly_budget_usd: float = 0.0
    max_open_proposals: int = 20
    per_source_daily_requests: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any, budgets: PinnedPolicy | None = None) -> AutonomyGuard:
        b = budgets.content if budgets is not None else budgets_policy().content
        llm_budget = min(
            float(getattr(settings, "reva_llm_monthly_budget_usd", 0) or 0),
            float((b.get("llm") or {}).get("monthly_usd", 0)),
        )
        return cls(
            kill_switch=bool(getattr(settings, "reva_autonomy_kill_switch", False)),
            mode=str(getattr(settings, "reva_autonomy_mode", "template_only")),
            llm_enabled=bool(getattr(settings, "reva_llm_enabled", False)),
            llm_monthly_budget_usd=llm_budget,
            max_open_proposals=int((b.get("autonomy") or {}).get("max_open_proposals", 20)),
            per_source_daily_requests=dict(b.get("per_source_daily_requests") or {}),
        )

    def blocked_reason(self) -> str | None:
        """Checked before every autonomy action.

        Combines the settings snapshot taken at construction with a *fresh* read of
        the live switches (``live_kill_reason``) on every call, so flipping the
        switch stops in-flight discovery before its next step and before promotion
        (review R4-13). The live read can only tighten, never loosen, the snapshot.
        """
        if self.kill_switch:
            return "REVA_AUTONOMY_KILL_SWITCH is on: all autonomy actions are blocked"
        if self.mode == "disabled":
            return "REVA_AUTONOMY_MODE=disabled: all autonomy actions are blocked"
        if self.mode != "template_only":
            return f"unknown autonomy mode {self.mode!r}: blocked"
        return live_kill_reason()

    def check(self, action: str) -> None:
        reason = self.blocked_reason()
        if reason:
            raise AutonomyBlocked(reason, action=action)

    def llm_blocked_reason(self) -> str | None:
        if self.blocked_reason():
            return self.blocked_reason()
        if not self.llm_enabled:
            return "blocked: llm disabled (REVA_LLM_ENABLED=false)"
        if self.llm_monthly_budget_usd <= 0:
            return "blocked: llm budget is 0"
        return None

    def check_backlog(self, conn: Connection) -> int:
        n = open_proposal_count(conn)
        if n >= self.max_open_proposals:
            raise AutonomyBlocked(
                f"proposal backlog is full ({n} open >= {self.max_open_proposals}); no new proposals",
                open=n,
                limit=self.max_open_proposals,
            )
        return n

    def source_request_limit(self, source_key: str) -> int:
        return int(self.per_source_daily_requests.get(source_key, 0))


_TRUE = {"1", "true", "yes", "on"}


def live_kill_reason(env: Mapping[str, str] | None = None) -> str | None:
    """Fresh (uncached) read of the kill switch, re-evaluated on every guard check.

    * ``REVA_AUTONOMY_KILL_SWITCH`` / ``REVA_AUTONOMY_MODE`` from the process
      environment as it is *now* (not the settings snapshot taken at start-up);
    * ``REVA_AUTONOMY_KILL_FILE``: if set and the file exists, autonomy is blocked.
      Operators can stop a running worker's autonomy with ``touch <file>`` (no
      redeploy); removing the file re-enables it.
    """
    e = os.environ if env is None else env
    if str(e.get("REVA_AUTONOMY_KILL_SWITCH", "")).strip().lower() in _TRUE:
        return "REVA_AUTONOMY_KILL_SWITCH is on: all autonomy actions are blocked"
    mode = str(e.get("REVA_AUTONOMY_MODE", "") or "").strip()
    if mode == "disabled":
        return "REVA_AUTONOMY_MODE=disabled: all autonomy actions are blocked"
    if mode and mode != "template_only":
        return f"unknown autonomy mode {mode!r}: blocked"
    kill_file = str(e.get("REVA_AUTONOMY_KILL_FILE", "") or "")
    if kill_file and os.path.lexists(kill_file):
        return "autonomy kill file is present: all autonomy actions are blocked"
    return None


def open_proposal_count(conn: Connection) -> int:
    """Proposals without a final gate decision (pass/fail) and without a human review."""
    return int(
        conn.execute(
            text(
                """SELECT count(*) FROM ops.proposal p
           WHERE NOT EXISTS (SELECT 1 FROM ops.gate_result g WHERE g.proposal_id = p.id
                             AND g.decision IN ('pass','fail'))
             AND NOT EXISTS (SELECT 1 FROM ops.proposal_review r WHERE r.proposal_id = p.id)"""
            )
        ).scalar_one()
    )


# --------------------------------------------------------------------------- tiers
T3_PATH_PREFIXES = (
    "src/reva/kernel",
    "src/reva/adapters/http",
    "src/reva/adapters/postgres",
    "src/reva/autonomy",
    "migrations",
    "registry/policies",
    "src/reva/settings.py",
    "src/reva/api/security",
    "contracts/",
    ".github",
    "dockerfile",
    "pyproject.toml",
    "uv.lock",
    "registry/sources",
    # (R4-11) the reviewed template that T1 auto-promotion trusts, and the code
    # its digest/behaviour depends on, are reviewed code: changing them is T3.
    "src/reva/adapters/connectors/eurostat_template.py",
    "src/reva/adapters/connectors/base.py",
    "src/reva/operations/budgets.py",
    # (R6-16) registry data and ontology code that T1 binding treats as ground truth
    # (concept semantic profiles, eurostat_geo aliases feeding eligibility scopes).
    "registry/concepts",
    "registry/entities",
    "registry/requirements",
    "src/reva/ontology",
    "src/reva/ports",
)
# (R6-16) mapping records change what source data means: never below named review (T2).
T2_PATH_PREFIXES = ("registry/mappings",)
T3_CAPABILITIES = {
    "paid_resources",
    "sensitive_data",
    "licence_change",
    "causal_claim",
    "destructive_migration",
    "secret_access",
    "network_any",
    "edit_policy",
    "edit_allowlist",
    "edit_gate",
}
T2_CAPABILITIES = {"execute_code", "new_model", "ontology_mapping", "new_parser", "legal_rule", "run_backtest"}
T1_CAPABILITIES = {"register_connector_version", "concept_eligibility"}
T0_CAPABILITIES = {"read", "discover", "report", "propose"}
CODE_SUFFIXES = (".py", ".pyx", ".so", ".sh", ".js", ".ts", ".sql", ".toml", ".cfg", ".ini", ".lock")


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def normalise_path(path: str) -> str | None:
    """Repository-relative POSIX path, case-folded, for tier derivation.

    Returns None for anything that cannot be normalised safely (absolute paths,
    backslashes, drive letters, NUL, or ``..`` escaping the repository): such
    paths are treated as protected (T3).
    """
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or "\x00" in path
        or path.startswith("/")
        or _DRIVE_RE.match(path)
    ):
        return None
    n = posixpath.normpath(path)
    if n in (".", "..") or n.startswith("../"):
        return None
    return n.lower()


def contains_code(changed_paths: tuple[str, ...], capabilities: tuple[str, ...]) -> bool:
    return "execute_code" in capabilities or any(
        (normalise_path(p) or p.lower()).endswith(CODE_SUFFIXES) for p in changed_paths
    )


def derive_tier(
    changed_paths: tuple[str, ...], capabilities: tuple[str, ...], scientific_change: bool, migration_required: bool
) -> tuple[int, list[str]]:
    """Minimum tier implied by what a proposal touches. The proposer cannot lower it."""
    reasons: list[str] = []
    tier = 0
    caps = set(capabilities)
    if migration_required:
        tier = 3
        reasons.append("schema migration required")
    for p in changed_paths:
        n = normalise_path(p)
        if n is None:
            tier = 3
            reasons.append(f"unnormalisable path {p[:200]!r} treated as protected")
        elif n.startswith(T3_PATH_PREFIXES):
            tier = 3
            reasons.append(f"protected path {n}")
    if caps & T3_CAPABILITIES:
        tier = 3
        reasons.append(f"T3 capabilities {sorted(caps & T3_CAPABILITIES)}")
    if tier < 3 and (contains_code(changed_paths, capabilities) or caps & T2_CAPABILITIES):
        tier = 2
        reasons.append("code, model, parser or ontology change")
    if tier < 2 and any((normalise_path(p) or "").startswith(T2_PATH_PREFIXES) for p in changed_paths):
        tier = 2
        reasons.append("mapping record change")
    if tier < 2 and scientific_change and not (caps <= T1_CAPABILITIES | T0_CAPABILITIES):
        tier = 2
        reasons.append("scientific change outside approved template scope")
    if tier < 1 and (caps & T1_CAPABILITIES or changed_paths):
        tier = 1
        reasons.append("new series / declarative manifest")
    unknown = caps - T0_CAPABILITIES - T1_CAPABILITIES - T2_CAPABILITIES - T3_CAPABILITIES
    if unknown:
        tier = max(tier, 2)
        reasons.append(f"unreviewed capabilities {sorted(unknown)}")
    return tier, reasons
