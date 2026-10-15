"""Bounded lineage graphs down to raw artifact bytes, storage location and selector.

estimate -> observation -> normalisation activity -> acquisition -> raw artifact
(sha256 + verified storage location + raw selector); sim_result -> run ->
scenario -> baseline release. A run also pins its model and ontology manifests
and its result (output) manifest; a scenario pins its inline canonical spec
artifact, its simulation context, its model version and its baseline release.
Every graph is bounded by ``max_nodes`` (``truncated`` is never silent).
"""

from __future__ import annotations

from collections import deque
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy import Connection, text

from reva.api import schemas as s
from reva.api.auth import require_scope
from reva.api.deps import SCENARIO_BANNER, Timer, origin_warnings, read_conn
from reva.api.errors import ApiProblem, NotFound

router = APIRouter(prefix="/v1", tags=["provenance"], dependencies=[Depends(require_scope("read"))])

MAX_DEPTH = 8


def _j(v: Any) -> Any:
    if isinstance(v, UUID):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


class Builder:
    def __init__(self, conn: Connection, max_nodes: int) -> None:
        self.conn = conn
        self.max_nodes = max_nodes
        self.nodes: dict[str, s.ProvenanceNode] = {}
        self.edges: list[s.ProvenanceEdge] = []
        self._edge_keys: set[tuple[str, str, str]] = set()
        self.queue: deque[tuple[str, str, int]] = deque()
        self.truncated = False
        self.origins: set[str] = set()
        self.scenario_context: Any = None  # context of the first scenario reached (simulation lineage)

    @staticmethod
    def key(kind: str, ident: Any) -> str:
        return f"{kind}:{ident}"

    def add(self, kind: str, ident: Any, label: str, /, **attrs: Any) -> str | None:
        k = self.key(kind, ident)
        if k in self.nodes:
            return k
        if len(self.nodes) >= self.max_nodes:
            self.truncated = True
            return None
        self.nodes[k] = s.ProvenanceNode(id=k, kind=kind, label=label,  # type: ignore[arg-type]
                                         attributes={a: _j(v) for a, v in attrs.items()})
        return k

    def edge(self, src: str | None, dst: str | None, relation: str, **attrs: Any) -> None:
        if src is None or dst is None or (src, dst, relation) in self._edge_keys:
            return
        self._edge_keys.add((src, dst, relation))
        self.edges.append(s.ProvenanceEdge(source=src, target=dst, relation=relation, attributes=attrs))

    def enqueue(self, kind: str, ident: Any, depth: int) -> None:
        if depth <= MAX_DEPTH:
            self.queue.append((kind, str(ident), depth))

    def one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        row = self.conn.execute(text(sql), params).mappings().one_or_none()
        return dict(row) if row else None

    def all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(text(sql), params).mappings()]

    # --- expansions ---------------------------------------------------
    def run(self, kind: str, ident: str) -> str:
        root = self.expand(kind, ident, 0, root=True)
        expanded = {self.key(kind, ident)}
        while self.queue:
            k, i, d = self.queue.popleft()
            if self.key(k, i) in expanded:
                continue
            expanded.add(self.key(k, i))
            self.expand(k, i, d)
        dangling = [e for e in self.edges if e.target not in self.nodes or e.source not in self.nodes]
        if dangling:
            self.truncated = True
            self.edges = [e for e in self.edges if e.target in self.nodes and e.source in self.nodes]
        return root

    def expand(self, kind: str, ident: str, depth: int, root: bool = False) -> str:
        fn = getattr(self, f"_{kind}")
        k: str | None = fn(ident, depth)
        if k is None and root:
            raise NotFound(f"{kind} not found", id=ident)
        return k or ""

    def _artifact(self, sha: str, depth: int) -> str | None:
        a = self.one("SELECT * FROM meta.artifact WHERE sha256 = :s", s=sha)
        if a is None:
            return None
        inline = self.one("SELECT octet_length(content) AS n FROM meta.artifact_inline WHERE artifact_sha256 = :s",
                          s=sha)
        k = self.add("artifact", sha, f"{a['media_type']} ({a['byte_size']} bytes)", sha256=sha,
                     media_type=a["media_type"], byte_size=a["byte_size"], schema_version=a["schema_version"],
                     privacy_class=a["privacy_class"], rights=a["rights"], stored_inline=inline is not None)
        for loc in self.all("""SELECT id, storage_uri, namespace, verified_byte_size, checksum_verified_at
                               FROM meta.artifact_location WHERE artifact_sha256 = :s ORDER BY recorded_at""", s=sha):
            lk = self.add("storage_location", loc["id"], loc["storage_uri"], storage_uri=loc["storage_uri"],
                          namespace=loc["namespace"], verified_byte_size=loc["verified_byte_size"],
                          checksum_verified_at=loc["checksum_verified_at"])
            self.edge(k, lk, "stored_at")
        return k

    def _activity(self, ident: str, depth: int) -> str | None:
        a = self.one("SELECT * FROM meta.activity WHERE id = :i", i=ident)
        if a is None:
            return None
        k = self.add("activity", ident, a["kind"], kind=a["kind"], origin=a["origin"], actor=a["actor"],
                     code_digest=a["code_digest"], environment_digest=a["environment_digest"],
                     parameters=a["parameters"], started_at=a["started_at"], ended_at=a["ended_at"])
        for inp in self.all("""SELECT ordinal, artifact_sha256, selector FROM meta.activity_input
                               WHERE activity_id = :i ORDER BY ordinal LIMIT 50""", i=ident):
            ak = self._artifact(inp["artifact_sha256"], depth + 1)
            self.edge(k, ak, "used", selector=inp["selector"], ordinal=inp["ordinal"])
        return k

    def _source(self, ident: str, depth: int) -> str | None:
        src = self.one("SELECT id, source_key, title, provider_url, rights FROM evidence.source WHERE id = :i",
                       i=ident)
        if src is None:
            return None
        return self.add("source", ident, src["source_key"], title=src["title"], provider_url=src["provider_url"],
                        rights=src["rights"])

    def _connector_version(self, ident: str, depth: int) -> str | None:
        cv = self.one("SELECT * FROM evidence.connector_version WHERE id = :i", i=ident)
        if cv is None:
            return None
        k = self.add("connector_version", ident, f"{cv['name']}@{cv['version']}", name=cv["name"],
                     version=cv["version"], code_digest=cv["code_digest"], spec_sha256=cv["spec_sha256"])
        self.edge(k, self._source(str(cv["source_id"]), depth + 1), "for_source")
        return k

    def _acquisition(self, ident: str, depth: int) -> str | None:
        a = self.one("SELECT * FROM evidence.acquisition WHERE id = :i", i=ident)
        if a is None:
            return None
        self.origins.add(a["origin"])
        rm = a["response_metadata"] or {}
        k = self.add("acquisition", ident, f"acquisition {a['retrieved_at']:%Y-%m-%d %H:%M}Z",
                     origin=a["origin"], retrieved_at=a["retrieved_at"], request_fingerprint=a["request_fingerprint"],
                     raw_sha256=a["raw_sha256"], item_key=rm.get("item_key"), url=rm.get("url"),
                     status=rm.get("status"), recorded_at=a["recorded_at"])
        self.edge(k, self._artifact(a["raw_sha256"], depth + 1), "raw_bytes")
        self.edge(k, self._connector_version(str(a["connector_version_id"]), depth + 1), "via_connector")
        ak = self._activity(str(a["provenance_activity_id"]), depth + 1)
        self.edge(k, ak, "generated_by")
        return k

    def _observation(self, ident: str, depth: int) -> str | None:
        o = self.one("""SELECT o.*, c.concept_id, src.source_key FROM evidence.observation o
                        JOIN ontology.concept_version c ON c.id = o.concept_version_id
                        JOIN evidence.source src ON src.id = o.source_id WHERE o.id = :i""", i=ident)
        if o is None:
            return None
        k = self.add("observation", ident, f"{o['logical_key']} r{o['revision_no']}", logical_key=o["logical_key"],
                     revision_no=o["revision_no"], value=o["value"], concept_id=o["concept_id"],
                     source_key=o["source_key"], recorded_at=o["recorded_at"],
                     source_published_at=o["source_published_at"], parser_version=o["parser_version"],
                     raw_selector=o["raw_selector"])
        raw_sha = (o["raw_selector"] or {}).get("artifact_sha256")
        self.edge(k, self._artifact(raw_sha, depth + 1) if raw_sha else None, "selected_from",
                  selector=o["raw_selector"])
        self.edge(k, self._activity(str(o["provenance_activity_id"]), depth + 1), "normalised_by")
        self.edge(k, self._acquisition(str(o["acquisition_id"]), depth + 1), "from_acquisition")
        if o["supersedes_id"]:
            self.edge(k, self.key("observation", o["supersedes_id"]), "revises")
            self.enqueue("observation", o["supersedes_id"], depth + 1)
        return k

    def _context(self, ident: str, depth: int) -> str | None:
        c = self.one("SELECT * FROM meta.context WHERE id = :i", i=ident)
        if c is None:
            return None
        self.origins.add(c["origin"])
        return self.add("context", ident, f"{c['mode']} context", mode=c["mode"], origin=c["origin"],
                        knowledge_cutoff=c["knowledge_cutoff"], manifest_sha256=c["manifest_sha256"])

    def _release(self, ident: str, depth: int) -> str | None:
        r = self.one("""SELECT r.*, p.sequence, p.published_at FROM belief.release r
                        LEFT JOIN ops.publication p ON p.release_id = r.id WHERE r.id = :i""", i=ident)
        if r is None:
            return None
        self.origins.add(r["origin"])
        k = self.add("release", ident, f"belief release #{r['sequence']}", sequence=r["sequence"],
                     published_at=r["published_at"], knowledge_cutoff=r["knowledge_cutoff"], origin=r["origin"],
                     method=r["method"])
        pinned = self.all("SELECT artifact_sha256, role FROM belief.release_artifact WHERE release_id = :i",
                          i=ident)
        # Dataset artifacts (V2 dataset inputs) are summarised on the release node, not expanded one by one:
        # they can number in the hundreds and are not inputs of any single estimate.
        dataset_pins = [x for x in pinned if str(x["role"]).startswith("dataset")]
        if dataset_pins and k is not None:
            self.nodes[k].attributes["dataset_artifacts_pinned"] = len(dataset_pins)
        for sha, role in [(r["manifest_sha256"], "release_manifest"),
                          (r["ontology_manifest_sha256"], "ontology_manifest"),
                          (r["model_manifest_sha256"], "model_manifest")] + [
            (x["artifact_sha256"], x["role"]) for x in pinned if not str(x["role"]).startswith("dataset")]:
            self.edge(k, self._artifact(sha, depth + 1), "pins", role=role)
        self.edge(k, self._context(str(r["context_id"]), depth + 1), "in_context")
        self.edge(k, self._activity(str(r["provenance_activity_id"]), depth + 1), "published_by")
        return k

    def _model_version(self, ident: str, depth: int) -> str | None:
        m = self.one("SELECT id, model_key, version, kind, spec_sha256, code_digest, approval_status "
                     "FROM model.version WHERE id = :i", i=ident)
        if m is None:
            return None
        return self.add("model_version", ident, f"{m['model_key']}@{m['version']}", kind=m["kind"],
                        spec_sha256=m["spec_sha256"], code_digest=m["code_digest"],
                        approval_status=m["approval_status"])

    def _estimate(self, ident: str, depth: int) -> str | None:
        e = self.one("""SELECT e.*, c.concept_id FROM belief.estimate e
                        JOIN ontology.concept_version c ON c.id = e.concept_version_id WHERE e.id = :i""", i=ident)
        if e is None:
            return None
        k = self.add("estimate", ident, f"{e['concept_id']} ({e['modality']})", modality=e["modality"],
                     value=e["value"], concept_id=e["concept_id"], uncertainty=e["uncertainty"])
        # The estimate's own inputs first (observation -> acquisition -> raw bytes), so the node budget is
        # spent on its lineage before the release's other pins.
        for inp in self.all("SELECT observation_id, prior_estimate_id, claim_id FROM belief.input "
                            "WHERE estimate_id = :i ORDER BY id LIMIT 50", i=ident):
            if inp["observation_id"]:
                self.edge(k, self._observation(str(inp["observation_id"]), depth + 1), "derived_from")
            elif inp["prior_estimate_id"]:
                self.edge(k, self.key("estimate", inp["prior_estimate_id"]), "derived_from")
                self.enqueue("estimate", inp["prior_estimate_id"], depth + 1)
            elif inp["claim_id"]:
                ck = self.add("claim", inp["claim_id"], "reviewed scientific claim")
                self.edge(k, ck, "informed_by")
        self.edge(k, self._model_version(str(e["method_version_id"]), depth + 1), "method")
        self.edge(k, self._release(str(e["release_id"]), depth + 1), "in_release")
        return k

    def _scenario(self, ident: str, depth: int) -> str | None:
        sc = self.one("""SELECT sc.*, mv.id AS model_version_id FROM sim.scenario sc
                         LEFT JOIN model.version mv ON mv.model_key = sc.spec->>'model_key'
                                                   AND mv.version = sc.spec->>'model_version'
                         WHERE sc.id = :i""", i=ident)
        if sc is None:
            return None
        if self.scenario_context is None:
            self.scenario_context = sc["context_id"]
        k = self.add("scenario", ident, sc["spec"].get("title", "scenario"), spec_digest=sc["spec_digest"],
                     spec_sha256=sc["spec_sha256"], created_by=sc["created_by"], recorded_at=sc["recorded_at"],
                     model_key=sc["spec"].get("model_key"), model_version=sc["spec"].get("model_version"),
                     fidelity=sc["spec"].get("fidelity"), knowledge_cutoff=sc["spec"].get("knowledge_cutoff"))
        # The canonical spec bytes are stored inline (meta.artifact_inline) by sim.create_scenario.
        self.edge(k, self._artifact(sc["spec_sha256"], depth + 1), "defined_by", role="scenario_spec")
        self.edge(k, self._context(str(sc["context_id"]), depth + 1), "in_context")
        self.edge(k, self._release(str(sc["baseline_release_id"]), depth + 1), "baseline_release")
        if sc["model_version_id"]:
            self.edge(k, self._model_version(str(sc["model_version_id"]), depth + 1), "uses_model")
        if sc["parent_scenario_id"]:
            self.edge(k, self.key("scenario", sc["parent_scenario_id"]), "derived_from")
            self.enqueue("scenario", sc["parent_scenario_id"], depth + 1)
        return k

    def _run(self, ident: str, depth: int) -> str | None:
        r = self.one("""SELECT r.*, o.status, o.output_manifest_sha256, o.output_digest, o.provenance_activity_id,
                               o.cache_hit_of
                        FROM sim.run r LEFT JOIN sim.run_outcome o ON o.run_id = r.id WHERE r.id = :i""", i=ident)
        if r is None:
            return None
        k = self.add("run", ident, f"run {r['fidelity']} ({r['status'] or 'pending'})", fidelity=r["fidelity"],
                     status=r["status"], job_id=r["job_id"], model_manifest_sha256=r["model_manifest_sha256"],
                     ontology_manifest_sha256=r["ontology_manifest_sha256"], seed_plan=r["seed_plan"],
                     execution_profile=r["execution_profile"], output_digest=r["output_digest"],
                     recorded_at=r["recorded_at"])
        self.edge(k, self._scenario(str(r["scenario_id"]), depth + 1), "of_scenario")
        self.edge(k, self._artifact(r["model_manifest_sha256"], depth + 1), "pins", role="model_manifest")
        self.edge(k, self._artifact(r["ontology_manifest_sha256"], depth + 1), "pins", role="ontology_manifest")
        if r["output_manifest_sha256"]:
            self.edge(k, self._artifact(r["output_manifest_sha256"], depth + 1), "produced", role="result_manifest")
        if r["provenance_activity_id"]:
            self.edge(k, self._activity(str(r["provenance_activity_id"]), depth + 1), "executed_by")
        if r["cache_hit_of"]:
            self.edge(k, self.key("run", r["cache_hit_of"]), "cache_hit_of")
            self.enqueue("run", r["cache_hit_of"], depth + 1)
        return k

    def _sim_result(self, ident: str, depth: int) -> str | None:
        r = self.one("SELECT * FROM sim.result WHERE id = :i", i=ident)
        if r is None:
            return None
        k = self.add("sim_result", ident, r["output_key"], output_key=r["output_key"], variant=r["variant"],
                     value=r["value"], uncertainty=r["uncertainty"])
        self.edge(k, self._run(str(r["run_id"]), depth + 1), "from_run")
        if r["artifact_sha256"]:
            self.edge(k, self._artifact(r["artifact_sha256"], depth + 1), "stored_as")
        self.edge(k, self._activity(str(r["provenance_activity_id"]), depth + 1), "generated_by")
        return k


@router.get("/provenance/{kind}/{ident}", response_model=s.ProvenanceResponse, responses={404: {"model": s.Problem}})
def get_provenance(
    request: Request,
    kind: s.ProvenanceKind = Path(...),
    ident: str = Path(..., max_length=100),
    max_nodes: int = Query(200, ge=10, le=1000),
) -> s.ProvenanceResponse:
    """Bounded lineage graph; ``truncated`` is set when ``max_nodes`` cut it."""
    t = Timer()
    if kind == "artifact":
        if len(ident) != 64 or any(c not in "0123456789abcdef" for c in ident):
            raise ApiProblem(422, "validation_error", "artifact id must be a lowercase sha256 hex digest")
    else:
        try:
            ident = str(UUID(ident))
        except ValueError:
            raise ApiProblem(422, "validation_error", f"{kind} id must be a UUID") from None
    with read_conn(request) as conn:
        b = Builder(conn, max_nodes)
        root = b.run(kind, ident)
    org = "none" if not b.origins else (next(iter(b.origins)) if len(b.origins) == 1 else "mixed")
    warnings = origin_warnings(org)
    if b.truncated:
        warnings.append(s.ApiWarning(code="truncated", message=f"Lineage bounded at max_nodes={max_nodes}"))
    simulation = kind in ("sim_result", "run", "scenario")
    mode = "simulation" if simulation else "belief" if kind in ("estimate", "release") else "evidence"
    if simulation:
        # Scenario lineage carries scenario values: same banner and modality as every simulation response.
        warnings.append(SCENARIO_BANNER)
    return s.ProvenanceResponse(
        data=s.ProvenanceGraph(root=root, nodes=list(b.nodes.values()), edges=b.edges, truncated=b.truncated,
                               max_nodes=max_nodes),
        context_id=b.scenario_context if simulation else None,
        context_mode=mode, origin=org,  # type: ignore[arg-type]
        modality="assumption_conditioned_scenario" if simulation else "metadata", warnings=warnings,
        cost=t.cost(len(b.nodes)),
    )
