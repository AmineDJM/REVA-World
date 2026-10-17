"""V4LINK shared pieces: the bridge predicates, a bounded KG loader, and the identity rules engine (V4, agent V4LINK).

Not an atlas source module (no ``atlas_`` prefix): imported by ``atlas_unsd_classif`` (classification structures and
concordances) and ``atlas_v4link_wikidata`` (Wikidata identifier bridges).

Predicates (namespaced, registered by the loaders; relation families in ``registry/kg/relations_v4.d/v4link.yaml``):

* ``v4link:class_parent`` -- a classification code is a subdivision of its parent code (HS subheading -> heading ->
  chapter; CPC subclass -> class -> group -> division -> section; ISIC class -> group -> division -> section; SITC).
  Family ``part_of``.
* ``v4link:corresponds_to`` -- a row of a published correspondence (concordance) table between two classifications
  (HS 2017 -> CPC 2.1, CPC 2.1 -> ISIC Rev.4, HS 2022 -> HS 2017 / HS 1992 / SITC Rev.4). The table's own partial
  flags / relationship are kept verbatim as qualifiers; REVA computes no weight or share. Not an identity, not a
  cause.
* ``v4link:wikidata_item`` -- an identifier of a domain authority (ChEBI, PubChem CID, NCBI taxon, ISO 3166-2, NUTS,
  ICD, MONDO, LEI...) is held by a Wikidata item (rules: ``registry/kg/identity_rules_v4.yaml``). Two identifiers on
  one item are the evidence of identity; no entity is merged.
* ``v4link:classified_as`` -- Wikidata states a classification code of an item (crude oil -> HS 2022 2709).

Nothing here maps by name or label similarity.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
RULES_PATH = REPO_ROOT / "registry" / "kg" / "identity_rules_v4.yaml"

P_CLASS_PARENT = "v4link:class_parent"
P_CORRESPONDS = "v4link:corresponds_to"
P_WIKIDATA_ITEM = "v4link:wikidata_item"
P_CLASSIFIED_AS = "v4link:classified_as"


def predicates() -> list[Any]:
    from reva.kg.model import KgProperty

    return [
        KgProperty(
            pid=P_CLASS_PARENT,
            value_kind="item",
            reva_predicate="classification_parent",
            label_en="is a subdivision of (classification)",
            label_fr="est une subdivision de (classification)",
            inverse_name="classification_child",
            inverse_label_en="has subdivision (classification)",
            inverse_label_fr="a pour subdivision (classification)",
        ),
        KgProperty(
            pid=P_CORRESPONDS,
            value_kind="item",
            reva_predicate="concordance_corresponds_to",
            label_en="corresponds to (published concordance table)",
            label_fr="correspond à (table de concordance publiée)",
            inverse_name="concordance_corresponds_from",
            inverse_label_en="corresponds to (published concordance table, read backwards)",
            inverse_label_fr="correspond à (table de concordance publiée, lue en sens inverse)",
        ),
        KgProperty(
            pid=P_WIKIDATA_ITEM,
            value_kind="item",
            reva_predicate="identifier_held_by_wikidata_item",
            label_en="identifier held by Wikidata item (evidence-based bridge)",
            label_fr="identifiant porté par l'élément Wikidata (pont fondé sur une preuve)",
            inverse_name="wikidata_item_holds_identifier",
            inverse_label_en="Wikidata item holds identifier",
            inverse_label_fr="l'élément Wikidata porte l'identifiant",
        ),
        KgProperty(
            pid=P_CLASSIFIED_AS,
            value_kind="item",
            reva_predicate="classified_as",
            label_en="classified under (code stated by the source)",
            label_fr="classé sous (code indiqué par la source)",
            inverse_name="classification_of",
            inverse_label_en="classification code of",
            inverse_label_fr="code de classification de",
        ),
    ]


# ============================================================================================== KG loading
def current_keys(engine: Any, source_key: str, origin: str, prefix: str) -> set[str]:
    from sqlalchemy import text

    like = f"{source_key}:{prefix}".replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with engine.connect() as c:
        return set(
            c.execute(
                text("""SELECT statement_key FROM kg.statement_current
                        WHERE source_key = :s AND origin = :o AND statement_key LIKE :p"""),
                {"s": source_key, "o": origin, "p": like},
            ).scalars()
        )


def existing_entities(engine: Any, qids: Sequence[str], origin: str) -> set[str]:
    """Which of ``qids`` are already KG entities of this origin (bounded chunks)."""
    from sqlalchemy import text

    out: set[str] = set()
    ids = sorted(set(qids))
    with engine.connect() as c:
        for i in range(0, len(ids), 5000):
            out.update(
                c.execute(
                    text("SELECT qid FROM kg.entity_current WHERE origin = :o AND qid = ANY(:q)"),
                    {"o": origin, "q": ids[i : i + 5000]},
                ).scalars()
            )
    return out


def load_kg(
    engine: Any,
    store: Any,
    *,
    entities: Sequence[Any],
    statements: Sequence[Any],
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    parameters: Mapping[str, Any],
    retract_prefix: str | None,
) -> dict[str, Any]:
    """Register the V4LINK predicates, then ``load_batch`` in bounded batches. Statements of this source whose key
    starts with ``<source_key>:<retract_prefix>`` and that the new build no longer has are retracted."""
    from reva.kg.model import (
        MAX_ENTITIES_PER_BATCH,
        MAX_RETRACTIONS_PER_BATCH,
        MAX_STATEMENTS_PER_BATCH,
        load_batch,
        register_properties,
        statement_key,
    )

    register_properties(engine, predicates())
    retract: list[str] = []
    if retract_prefix is not None:
        new = {statement_key(s, source_key) for s in statements}
        retract = sorted(current_keys(engine, source_key, origin, retract_prefix) - new)
    n = max(
        1,
        -(-len(statements) // MAX_STATEMENTS_PER_BATCH),
        -(-len(entities) // MAX_ENTITIES_PER_BATCH),
        -(-len(retract) // MAX_RETRACTIONS_PER_BATCH),
    )
    reports = []
    for i in range(n):
        rep = load_batch(
            engine,
            store,
            entities=entities[i * MAX_ENTITIES_PER_BATCH : (i + 1) * MAX_ENTITIES_PER_BATCH],
            statements=statements[i * MAX_STATEMENTS_PER_BATCH : (i + 1) * MAX_STATEMENTS_PER_BATCH],
            source_key=source_key,
            raw_sha256s=sorted(set(raw_sha256s)),
            retrieved_at=retrieved_at,
            origin=origin,
            retract_keys=retract[i * MAX_RETRACTIONS_PER_BATCH : (i + 1) * MAX_RETRACTIONS_PER_BATCH],
            parameters=dict(parameters),
        )
        reports.append(rep.as_dict())
    keys = (
        "entities_inserted",
        "entities_unchanged",
        "entities_conflicting",
        "statements_inserted",
        "statements_new_revision",
        "statements_unchanged",
        "statements_stale",
        "statements_conflicting",
        "retractions_inserted",
    )
    rejected: Counter[str] = Counter()
    for r in reports:
        rejected.update(r.get("rejected_counts") or {})
    return {
        **{k: sum(int(r.get(k) or 0) for r in reports) for k in keys},
        "batches": len(reports),
        "load_ids": [r.get("load_id") for r in reports],
        "rejected_counts": dict(rejected),
        "rejected_sample": [x for r in reports for x in (r.get("rejected") or [])][:20],
        "retracted_keys_sample": retract[:20],
    }


# ============================================================================================== identity rules
NORMALIZERS: dict[str, Any] = {
    "strip_dots": lambda v: v.replace(".", ""),
    "strip_leading_zeros": lambda v: v.lstrip("0"),
    "strip_prefix_chebi": lambda v: re.sub(r"^CHEBI[:_]", "", v),
    "strip_prefix_mondo": lambda v: re.sub(r"^MONDO[:_]", "", v),
    "strip_prefix_doid": lambda v: re.sub(r"^DOID[:_]", "", v),
    "strip_prefix_uberon": lambda v: re.sub(r"^UBERON[:_]", "", v),
    "strip_prefix_hgnc": lambda v: re.sub(r"^HGNC:", "", v),
}
RELATIONS = ("identity", "classification")


@dataclass(frozen=True)
class Rule:
    property: str
    label: str
    prefix: str
    relation: str
    value_re: re.Pattern[str]
    normalize: str | None = None
    one_value_per_holder: bool = True
    max_items_per_value: int | None = 1
    kind_hint: str | None = None
    status: str = "active"
    note: str = ""

    def key(self, value: str) -> str | None:
        """The KG id of a stated value, or None when it does not match the rule's format (never repaired)."""
        v = value.strip()
        if self.normalize:
            v = NORMALIZERS[self.normalize](v)
        return f"{self.prefix}{v}" if self.value_re.match(v) else None


@dataclass(frozen=True)
class Pack:
    id: str
    properties: tuple[str, ...]
    require_two: bool = False
    min_sitelinks: int | None = None
    with_country: bool = False
    note: str = ""


@dataclass
class Rules:
    version: int
    rules: dict[str, Rule]
    packs: dict[str, Pack]


def load_rules(path: Path = RULES_PATH) -> Rules:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, Mapping) or doc.get("version") != 1:
        raise ValueError(f"{path}: expected a mapping with version 1")
    rules: dict[str, Rule] = {}
    for r in doc["rules"]:
        pid = str(r["property"])
        if not re.match(r"^P[1-9][0-9]{0,9}$", pid) or pid in rules:
            raise ValueError(f"{path}: bad or duplicate property {pid}")
        if r["relation"] not in RELATIONS:
            raise ValueError(f"{path}: {pid}: relation must be one of {RELATIONS}")
        if r.get("normalize") and r["normalize"] not in NORMALIZERS:
            raise ValueError(f"{path}: {pid}: unknown normalizer {r['normalize']}")
        if not re.match(r"^[a-z][a-z0-9_]{1,31}:$", str(r["prefix"])):
            raise ValueError(f"{path}: {pid}: prefix must look like 'scheme:'")
        if r["relation"] == "identity" and not (r.get("one_value_per_holder", True) and r.get("max_items_per_value")):
            raise ValueError(f"{path}: {pid}: an identity rule needs one_value_per_holder and max_items_per_value")
        rules[pid] = Rule(
            property=pid,
            label=str(r["label"]),
            prefix=str(r["prefix"]),
            relation=str(r["relation"]),
            value_re=re.compile(str(r["value_re"])),
            normalize=r.get("normalize"),
            one_value_per_holder=bool(r.get("one_value_per_holder", True)),
            max_items_per_value=r.get("max_items_per_value"),
            kind_hint=r.get("kind_hint"),
            status=str(r.get("status", "active")),
            note=str(r.get("note", "")),
        )
    packs: dict[str, Pack] = {}
    for p in doc["packs"]:
        props = tuple(str(x) for x in p["properties"])
        missing = [x for x in props if x not in rules]
        if missing or p["id"] in packs:
            raise ValueError(f"{path}: pack {p['id']}: unknown properties {missing} or duplicate id")
        packs[str(p["id"])] = Pack(
            id=str(p["id"]),
            properties=props,
            require_two=bool(p.get("require_two", False)),
            min_sitelinks=p.get("min_sitelinks"),
            with_country=bool(p.get("with_country", False)),
            note=str(p.get("note", "")),
        )
    return Rules(version=int(doc["version"]), rules=rules, packs=packs)


@dataclass(frozen=True)
class Held:
    """One best-rank Wikidata statement: item holds value of property."""

    item: str
    property: str
    value: str
    statement_id: str
    rank: str


@dataclass
class Bridges:
    links: list[tuple[Held, str]] = field(default_factory=list)  # (statement, KG key)
    report: dict[str, Any] = field(default_factory=dict)


def best_rank(rows: Sequence[Held]) -> list[Held]:
    """Wikidata's truthy view: per (item, property) the preferred statements if any, else the normal ones."""
    by: dict[tuple[str, str], list[Held]] = defaultdict(list)
    for h in rows:
        by[(h.item, h.property)].append(h)
    out: list[Held] = []
    for hs in by.values():
        pref = [h for h in hs if h.rank == "preferred"]
        out.extend(pref or [h for h in hs if h.rank == "normal"])
    return out


def derive_bridges(
    rows: Sequence[Held], rules: Mapping[str, Rule], global_duplicates: Mapping[str, set[str]] | None = None
) -> Bridges:
    """Apply the identity rules to best-rank statements: malformed values, ambiguous holders (an item with several
    values of one property) and ambiguous values (a value on several items) are reported, never linked.

    ``rows`` may be a filtered subset of Wikidata (a pack with ``min_sitelinks``): ``global_duplicates`` (property ->
    KG keys held by several items anywhere in Wikidata) then completes the ambiguity check for values whose other
    holders were not fetched."""
    dups = global_duplicates or {}
    rep: dict[str, Any] = {"by_property": {}}
    out = Bridges()
    best = best_rank(rows)
    by_prop: dict[str, list[Held]] = defaultdict(list)
    for h in best:
        by_prop[h.property].append(h)
    for pid in sorted(by_prop):
        rule = rules.get(pid)
        stats: Counter[str] = Counter()
        samples: dict[str, list[Any]] = defaultdict(list)
        if rule is None or rule.status != "active":
            rep["by_property"][pid] = {"skipped": "no active rule", "statements": len(by_prop[pid])}
            continue
        keyed: list[tuple[Held, str]] = []
        for h in by_prop[pid]:
            k = rule.key(h.value)
            if k is None:
                stats["malformed"] += 1
                if len(samples["malformed"]) < 10:
                    samples["malformed"].append({"item": h.item, "value": h.value})
                continue
            keyed.append((h, k))
        per_item: dict[str, set[str]] = defaultdict(set)
        per_value: dict[str, set[str]] = defaultdict(set)
        for h, k in keyed:
            per_item[h.item].add(k)
            per_value[k].add(h.item)
        seen: set[tuple[str, str]] = set()
        for h, k in keyed:
            if rule.one_value_per_holder and len(per_item[h.item]) > 1:
                stats["ambiguous_holder"] += 1
                if len(samples["ambiguous_holder"]) < 10:
                    samples["ambiguous_holder"].append({"item": h.item, "values": sorted(per_item[h.item])[:5]})
                continue
            if rule.max_items_per_value is not None and (
                len(per_value[k]) > int(rule.max_items_per_value) or k in dups.get(pid, set())
            ):
                stats["ambiguous_value"] += 1
                if len(samples["ambiguous_value"]) < 10:
                    samples["ambiguous_value"].append({"value": k, "items": sorted(per_value[k])[:5]})
                continue
            if (h.item, k) in seen:
                stats["duplicate"] += 1
                continue
            seen.add((h.item, k))
            out.links.append((h, k))
            stats["linked"] += 1
        rep["by_property"][pid] = {
            "statements": len(by_prop[pid]),
            **dict(stats),
            "samples": {k: v for k, v in samples.items() if v},
        }
    rep["linked"] = len(out.links)
    out.report = rep
    return out
