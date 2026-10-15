"""Population recipe service: Eurostat Jan-1 age x recorded-sex leaf cells ->
recipe -> weighted materialisation (Parquet artifact, never PostgreSQL rows) ->
diagnostics -> coarsen/refine compression certificates.

The trade stress test is deliberately NOT coupled to this population: dividing a
national import-bill change by synthetic people is not a distributional result.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Engine, text

from reva.adapters.postgres import repositories as repo
from reva.belief.selection import eligible_latest
from reva.compression.refine import UnsupportedRefinement, refine
from reva.compression.stratified import coarsen
from reva.kernel.errors import IncompatibleSemantics, MissingInput, ValidationProblem
from reva.operations.context import JobContext
from reva.population.diagnostics import diagnose
from reva.population.materialise import materialise, table_logical_hash, to_parquet_bytes
from reva.population.recipe import SourceCell, build_recipe
from reva.ports.storage import ArtifactStore

CONCEPT = "demography.population.jan1_age_sex"
FIVE_YEAR_BANDS: list[tuple[str, int, int | None]] = [
    (f"Y{lo}-{lo + 4}", lo, lo + 4) for lo in range(0, 85, 5)
] + [("Y_GE85", 85, None)]


def _integral_count(value: Any, source_key: str) -> int:
    """A person count published as a decimal string ("123" or "123.0"); a fractional count is rejected."""
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValidationProblem("population count is not a number", source_key=source_key) from None
    if not d.is_finite() or d != d.to_integral_value():
        raise ValidationProblem("population count is not an integer", source_key=source_key, value=str(value))
    return int(d)


def load_cells(engine: Engine, *, geo_entity: str, year: int, cutoff: datetime, origin: str) -> list[SourceCell]:
    from reva.kernel.ids import entity_id

    subject = entity_id(geo_entity)
    with engine.connect() as conn:
        rows = eligible_latest(conn, cutoff, [origin], [CONCEPT])
        supports = {
            r["id"]: r
            for r in conn.execute(
                text("SELECT id, resolution, temporal_extent FROM world.support WHERE id = ANY(:ids)"),
                {"ids": list({r["support_id"] for r in rows})},
            ).mappings()
        }
    # All cells (leaf, subtotal and total, incl. TOTAL/Y_LT1..Y99/Y_OPEN/UNK and sex
    # T/M/F) are passed through: build_recipe selects the disjoint leaf partition
    # itself and audits it against the totals. cell_type from the connector is not
    # trusted for the partition.
    cells = []
    for r in rows:
        if r["subject_id"] != subject:
            continue
        sup = supports[r["support_id"]]
        start = (sup["temporal_extent"] or {}).get("start")
        if not start or date.fromisoformat(start) != date(year, 1, 1):
            continue
        res = sup["resolution"] or {}
        v = r["value"]
        count = _integral_count(v["value"], r["logical_key"]) if v.get("kind") == "decimal" else None
        # Eurostat OBS_FLAG codes: the template stores them as one string of flag
        # characters (e.g. "p", "ep", ""); accept a list too. One code per flag.
        st = (r.get("source_flags") or {}).get("status") or ""
        raw_codes = st if isinstance(st, (list, tuple)) else list(str(st))
        flags = tuple(sorted({str(c).strip() for c in raw_codes if str(c).strip()}))
        cells.append(SourceCell(age_code=str(res.get("age")), sex_code=str(res.get("sex")), count=count,
                                flags=flags, source_key=r["logical_key"]))
    if not cells:
        raise MissingInput(f"no eligible {CONCEPT} cells for {geo_entity} {year} at cutoff",
                           input=CONCEPT, geography=geo_entity, year=year)
    return cells


def build_population(
    worker: Engine, store: ArtifactStore, *, geo_entity: str = "FRA", year: int = 2023, n_records: int = 100_000,
    seed: int = 20260101, origin: str = "live", cutoff: datetime | None = None, actor: str = "worker:population",
) -> dict[str, Any]:
    cutoff = cutoff or datetime.now(UTC)
    cells = load_cells(worker, geo_entity=geo_entity, year=year, cutoff=cutoff, origin=origin)
    recipe = build_recipe(cells, geography=geo_entity, reference_date=date(year, 1, 1),
                          source_ref={"concept": CONCEPT, "dataset": "eurostat demo_pjan",
                                      "cutoff": cutoff.isoformat()},
                          seed=seed)
    table = materialise(recipe, n_records)
    diag = diagnose(table, recipe)
    pq_obj = store.put_bytes(to_parquet_bytes(table), "models", "application/vnd.apache.parquet")
    # Compression is optional: when it does not apply (unknown Y_OPEN bound, a band
    # that would split a leaf, ...) it is recorded as not_applicable with the reason;
    # the population itself is still published. Recipe errors (e.g.
    # InconsistentSourceTotals) are not caught: the job fails with the typed error.
    compression: dict[str, Any] = {}
    steps: list[tuple[str, Any, Any]] = []  # (operation, result, stored object)
    try:
        coarse = coarsen(table, FIVE_YEAR_BANDS, task_scope="age_band_reporting")
    except ValidationProblem as e:
        compression["coarsen"] = {"status": "not_applicable", "reason": e.message}
        compression["refine"] = {"status": "not_applicable", "reason": "requires a coarsened table"}
    else:
        coarse_obj = store.put_bytes(to_parquet_bytes(coarse.table), "models", "application/vnd.apache.parquet")
        steps.append(("coarsen", coarse, coarse_obj))
        compression["coarsen"] = {"status": "applied", "output_sha256": coarse_obj.sha256}
        try:
            refined = refine(coarse.table, recipe, "age", task_scope="age_band_reporting")
        except (ValidationProblem, UnsupportedRefinement, IncompatibleSemantics) as e:
            compression["refine"] = {"status": "not_applicable", "reason": e.message}
        else:
            refined_obj = store.put_bytes(to_parquet_bytes(refined.table), "models",
                                          "application/vnd.apache.parquet")
            steps.append(("refine", refined, refined_obj))
            compression["refine"] = {"status": "applied", "output_sha256": refined_obj.sha256}
    diag = {**diag, "compression": compression}
    with worker.begin() as conn:
        act = repo.simple_activity("population.materialise", "model", actor,
                                   {"geo": geo_entity, "year": year, "n_records": n_records, "seed": seed})
        recipe_sha = repo.store_json_artifact(conn, store, recipe.to_dict(), "models",
                                              schema_version="population-recipe/1",
                                              privacy_class="synthetic_public_aggregate_derived")
        for obj, logical in [(pq_obj, table_logical_hash(table))] + [
                (o, table_logical_hash(res.table)) for _op, res, o in steps]:
            repo.register_artifact(conn, obj, schema_version="population-records/1",
                                   rights={"licence": "derived from Eurostat (CC BY 4.0)"},
                                   privacy_class="synthetic_public_aggregate_derived", logical_sha256=logical)
        repo.record_activity(conn, act, outputs=[recipe_sha, pq_obj.sha256, *(o.sha256 for _op, _r, o in steps)])
        ctx_id = repo.create_context(conn, mode="population", origin=origin, manifest_sha256=recipe_sha,
                                     knowledge_cutoff=cutoff)
        pid = conn.execute(
            text(
                """INSERT INTO model.population_dataset(context_id, recipe_sha256, materialisation_sha256,
                     supported_attributes, record_count, diagnostics, seed_plan, privacy_class, provenance_activity_id)
                   VALUES (:c, :r, :m, CAST(:sa AS jsonb), :n, CAST(:d AS jsonb), CAST(:sp AS jsonb),
                           'synthetic_public_aggregate_derived', :a) RETURNING id"""
            ),
            {"c": ctx_id, "r": recipe_sha, "m": pq_obj.sha256, "sa": json.dumps(["age", "sex"]), "n": table.num_rows,
             "d": json.dumps(diag, default=str), "sp": json.dumps({"seed": seed, "allocator": "largest_remainder"}),
             "a": act.id},
        ).scalar_one()
        for op, res, obj in steps:
            conn.execute(
                text(
                    """INSERT INTO model.compression_certificate(population_dataset_id, operation, task_scope,
                         certificate, output_sha256, provenance_activity_id)
                       VALUES (:p, :o, CAST(:t AS jsonb), CAST(:c AS jsonb), :s, :a)"""
                ),
                {"p": pid, "o": op, "t": json.dumps({"task": "age_band_reporting"}),
                 "c": json.dumps(res.certificate.to_dict(), default=str), "s": obj.sha256, "a": act.id},
            )
    return {"population_dataset_id": str(pid), "context_id": str(ctx_id), "records": table.num_rows,
            "recipe_sha256": recipe_sha, "materialisation_sha256": pq_obj.sha256,
            "ess": diag.get("effective_sample_size"), "leaf_cells": len(recipe.leaf_cells),
            "totals_status": recipe.totals_status, "compression": compression}


def handle_population(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    return build_population(
        ctx.worker_engine, ctx.store,
        geo_entity=payload.get("geo", "FRA"), year=int(payload.get("year", 2023)),
        n_records=int(payload.get("n_records", 100_000)), seed=int(payload.get("seed", 20260101)),
        origin=payload.get("origin", "live"),
    )
