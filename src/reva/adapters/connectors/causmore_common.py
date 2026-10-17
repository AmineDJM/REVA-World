"""Shared helpers of the CAUSMORE connectors (agent CAUSMORE, 2026-10-17).

Connectors: :mod:`atlas_cowwars` (COW War Data v4: inter-, extra-, intra- (v4.1) and non-state wars),
:mod:`atlas_pax` (PA-X Peace Agreements Database v10) and :mod:`atlas_idmc` (IDMC GIDD disaster events).

* :func:`relations` -- ``registry/causal/relations_causmore.yaml`` (owner CAUSMORE): pinned codebook documents, the
  verbatim definitions passed as ``Codebook.definitions`` and, per dataset variable, whether it becomes a T1 claim or
  a context statement, with the reason.
* Claims use CAUSDATA's neutral records and delivery (:class:`causdata_common.ClaimRecord`,
  :class:`causdata_common.ClaimDelivery`, :func:`causdata_common.deliver_claims`): exactly CAUSCORE's
  ``reva.causal.load_dataset_claims`` interface, loaded when the causal schema exists, else staged or reported.
* :data:`CAUSMORE_PROPERTIES` -- the dataset predicates of these connectors (none causal: causal links are claims) and
  :func:`load_kg`, which registers them and then loads through CAUSDATA's bounded loader.

Nothing here makes a request: connectors fetch through ``run.get`` only (atlas platform, invariant 30).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any

from reva.adapters.connectors import causdata_common as cd
from reva.adapters.connectors.atlas_geopol_common import KgEntity, KgStatement

RELATIONS_FILE = "causal/relations_causmore.yaml"


@lru_cache(maxsize=1)
def relations() -> dict[str, Any]:
    import yaml

    from reva.ontology.registry import REGISTRY_ROOT

    doc = yaml.safe_load((REGISTRY_ROOT / RELATIONS_FILE).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("maintainer") != "CAUSMORE":
        raise ValueError("registry/causal/relations_causmore.yaml: unexpected content")
    return doc


def dataset_entry(key: str) -> dict[str, Any]:
    out: dict[str, Any] = relations()["datasets"][key]
    return out


def document(key: str) -> dict[str, Any]:
    out: dict[str, Any] = relations()["documents"][key]
    return out


def value_label(dataset: str, variable: str, code: Any) -> str | None:
    """Codebook label of a coded value (``None`` for an empty / uncoded value)."""
    s = "" if code is None else str(code).strip()
    if not s.lstrip("-").isdigit():
        return None
    label = dataset_entry(dataset).get("values", {}).get(variable, {}).get(int(s))
    return None if label is None else str(label)


def codebook(dataset: str, documents_sha256: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``Codebook`` fields for ``load_dataset_claims`` (name, version + document sha256s, url, verbatim definitions)."""
    e = dataset_entry(dataset)
    doc = document(e["codebook_document"])
    version = str(e["dataset_revision"])
    shas = dict(documents_sha256 or {})
    if shas:
        version += " (" + ", ".join(f"{k} sha256 {v[:12]}" for k, v in sorted(shas.items())) + ")"
    return {
        "name": e["codebook_name"],
        "version": version,
        "url": doc.get("download_url") or doc.get("url") or "",
        "definitions": {k: str(v["text"]) for k, v in e["definitions"].items()},
    }


def check_pdf(key: str, body: bytes) -> dict[str, Any]:
    """sha256 of a fetched codebook PDF vs the pinned one (a changed codebook withholds that dataset's claims)."""
    sha = hashlib.sha256(body).hexdigest()
    pinned = document(key)["sha256"]
    return {"document": key, "sha256": sha, "pinned_sha256": pinned, "matches": sha == pinned}


def check_texts(dataset: str, texts: Iterable[str]) -> dict[str, Any]:
    """Every definition of ``dataset`` must be one of ``texts`` verbatim (whitespace normalised): used for a codebook
    that lives inside the data file (IDMC's README sheet), whose bytes change with every data release."""

    def norm(s: str) -> str:
        return " ".join(str(s).split())

    have = {norm(t) for t in texts if t}
    missing = [k for k, v in dataset_entry(dataset)["definitions"].items() if norm(v["text"]) not in have]
    digest = hashlib.sha256("\n".join(sorted(have)).encode("utf-8")).hexdigest()
    return {"document": dataset_entry(dataset)["codebook_document"], "sha256": digest, "matches": not missing,
            "missing_definitions": missing}


def delivery(
    dataset: str,
    *,
    claims: Sequence[cd.ClaimRecord],
    raw_sha256s: Sequence[str],
    retrieved_at: Any,
    origin: str,
    documents_sha256: Mapping[str, str],
) -> cd.ClaimDelivery:
    e = dataset_entry(dataset)
    return cd.ClaimDelivery(
        source_key=e["source_key"],
        dataset=e["dataset"],
        dataset_revision=str(e["dataset_revision"]),
        codebook=codebook(dataset, documents_sha256),
        raw_sha256s=list(raw_sha256s),
        retrieved_at=retrieved_at,
        origin=origin,
        claims=list(claims),
        complete=True,
    )


def string_stmt(
    key: str, subject: str, pid: str, text: str, *, raw_sha256: str, selector: Mapping[str, Any], **qual: Any
) -> KgStatement:
    return cd.string_stmt(key, subject, pid, text, raw_sha256=raw_sha256, selector=selector, **qual)


def item_stmt(
    key: str, subject: str, pid: str, obj: str, *, raw_sha256: str, selector: Mapping[str, Any], **qual: Any
) -> KgStatement:
    return KgStatement(
        key=key,
        subject=subject,
        property=pid,
        object_qid=obj,
        qualifiers={k: v for k, v in qual.items() if v is not None},
        raw_sha256=raw_sha256,
        selector=dict(selector),
    )


def check_unique(entities: Sequence[KgEntity], statements: Sequence[KgStatement]) -> None:
    cd.check_unique_keys(statements)
    seen: set[str] = set()
    for e in entities:
        if e.qid in seen:
            raise ValueError(f"duplicate entity {e.qid}")
        seen.add(e.qid)


# ----------------------------------------------------------------------------- KG predicates

#: pid -> (value kind, reva predicate, en label, fr label, inverse name, inverse en label). None is causal.
CAUSMORE_PROPERTIES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    "causmore:event_type": (
        "string",
        "causmore_event_type",
        "record type (CAUSMORE dataset kind)",
        "type d'enregistrement (jeu de données CAUSMORE)",
        None,
        None,
    ),
    "cowwar:war_type": ("string", "cow_war_type", "COW war type (WarType)", "type de guerre COW", None, None),
    "cowwar:outcome": ("string", "cow_war_outcome", "COW war outcome (Outcome)", "issue de la guerre (COW)", None, None),
    "cowwar:transformed_into": (
        "item",
        "cow_war_transformed_into",
        "war transformed into (COW TransTo / TransFrom; not causal)",
        "guerre transformée en (COW ; non causal)",
        "cow_war_transformed_from",
        "war transformed from (COW)",
    ),
    "cowwar:side_a": ("string", "cow_war_side_a", "side A (COW, name as coded)", "camp A (COW)", None, None),
    "cowwar:side_b": ("string", "cow_war_side_b", "side B (COW, name as coded)", "camp B (COW)", None, None),
    "cowwar:initiator": (
        "string",
        "cow_war_initiator",
        "initiator (COW Initiator; a participant role, not a cause)",
        "initiateur (COW ; rôle, pas une cause)",
        None,
        None,
    ),
    "pax:stage": ("string", "pax_stage", "agreement stage (PA-X Stage)", "étape de l'accord (PA-X)", None, None),
    "pax:status": ("string", "pax_status", "agreement status (PA-X Status)", "statut de l'accord (PA-X)", None, None),
    "pax:agreement_type": (
        "string",
        "pax_agreement_type",
        "agreement/conflict type (PA-X Agtp)",
        "type d'accord / de conflit (PA-X)",
        None,
        None,
    ),
    "pax:parties": ("string", "pax_parties", "parties as written (PA-X Part)", "parties (texte PA-X)", None, None),
    "pax:ucdp_agreement_id": (
        "string",
        "pax_ucdp_agreement_id",
        "UCDP peace agreement id (PA-X UcdpAgr)",
        "identifiant UCDP de l'accord (PA-X)",
        None,
        None,
    ),
    "pax:pam_agreement_id": (
        "string",
        "pax_pam_agreement_id",
        "Peace Accords Matrix id (PA-X PamAgr)",
        "identifiant Peace Accords Matrix (PA-X)",
        None,
        None,
    ),
    "pax:ucdp_conflict": (
        "item",
        "pax_ucdp_conflict",
        "UCDP conflict addressed (PA-X UcdpCon)",
        "conflit UCDP visé (PA-X)",
        "pax_agreement_of_ucdp_conflict",
        "has PA-X peace agreement",
    ),
    "pax:cow_war": (
        "item",
        "pax_cow_war",
        "COW war addressed (PA-X CowWar)",
        "guerre COW visée (PA-X)",
        "pax_agreement_of_cow_war",
        "has PA-X peace agreement (COW war)",
    ),
    "pax:location": ("string", "pax_location", "location (PA-X Loc ISO, unmapped)", "lieu (PA-X)", None, None),
    "idmc:hazard_sub_type": (
        "string",
        "idmc_hazard_sub_type",
        "hazard sub-type (IDMC, CRED EM-DAT classification)",
        "sous-type d'aléa (IDMC, classification EM-DAT)",
        None,
        None,
    ),
    "idmc:glide_number": ("string", "idmc_glide_number", "GLIDE number (IDMC)", "numéro GLIDE (IDMC)", None, None),
    "idmc:event_code": (
        "string",
        "idmc_event_code",
        "other event code (IDMC Event Codes)",
        "autre code d'événement (IDMC)",
        None,
        None,
    ),
    "idmc:internal_displacements": (
        "quantity",
        "idmc_internal_displacements",
        "internal displacements (IDMC, flows over the reporting year)",
        "déplacements internes (IDMC, flux de l'année)",
        None,
        None,
    ),
    "idmc:country": ("string", "idmc_country", "country (IDMC ISO3, unmapped)", "pays (IDMC, non apparié)", None, None),
}
WIKIDATA_PIDS = ("P17",)


def ensure_kg_properties(engine: Any) -> None:
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import KgProperty, register_properties

    props = [p for p in kg_properties(load_allowlist()) if p.pid in WIKIDATA_PIDS]
    if len(props) != len(WIKIDATA_PIDS):
        raise ValueError(f"{WIKIDATA_PIDS} must all be in registry/kg/properties.yaml")
    props += [
        KgProperty(
            pid=p, value_kind=k, reva_predicate=rp, label_en=le, label_fr=lf, inverse_name=inv, inverse_label_en=ile
        )
        for p, (k, rp, le, lf, inv, ile) in CAUSMORE_PROPERTIES.items()
    ]
    register_properties(engine, props)


def load_kg(
    engine: Any,
    store: Any,
    entities: Sequence[KgEntity],
    statements: Sequence[KgStatement],
    *,
    source_key: str,
    source_revision: str,
    raw_sha256s: Sequence[str],
    retrieved_at: Any,
    origin: str,
) -> dict[str, Any]:
    """Register the CAUSMORE predicates (and, through CAUSDATA's loader, CAUSDATA's), then load in bounded batches."""
    ensure_kg_properties(engine)
    return cd.load_kg(
        engine,
        store,
        entities,
        statements,
        source_key=source_key,
        source_revision=source_revision,
        raw_sha256s=raw_sha256s,
        retrieved_at=retrieved_at,
        origin=origin,
    )


def country_qids() -> dict[str, str]:
    """ISO3 -> country QID (WDHARVEST's ``registry/entities/country_qids.json``; ``{}`` when absent)."""
    from reva.adapters.connectors.atlas_geo_common import load_country_qids

    return dict(load_country_qids())
