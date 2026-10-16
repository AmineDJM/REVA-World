"""Evidence-based ``same_as`` links between records of the same real event (agent EVENTS).

Two KG entities are linked only when evidence says they denote the same event:

* **shared identifier** — both carry the same value of an identifier that the rules in
  ``registry/events/same_as_rules.yaml`` declare an event key (e.g. UNTC's MTDSG treaty number), or a dataset
  record's own id equals the value of a *verified* Wikidata identifier property on an item;
* **evidenced mapping table** — a reviewed table that maps a dataset key to a Wikidata QID with per-entry
  evidence (e.g. A-TREATIES' ``registry/entities/treaty_qids.json``, entries with ``status: verified``).

Never by name or date similarity. A value held by more than one entity on one side is *ambiguous*: reported, never
linked. Links are KG statements ``events:same_as`` (source ``reva.events.same_as``) whose qualifiers carry the
evidence and declare the derivation (``verification: derived``); the raw artifact is the canonical JSON evidence
document of the run. A later run retracts links whose evidence disappeared (``removed_at_source``). Nothing is
merged: the event view shows both records and the link.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Connection, Engine, text

from reva.kg.model import KgProperty, KgStatement, LoadReport, load_batch, register_properties

REPO_ROOT = Path(__file__).resolve().parents[3]
RULES_PATH = REPO_ROOT / "registry" / "events" / "same_as_rules.yaml"
SOURCE_KEY = "reva.events.same_as"
METHOD = "reva.events.same_as/1"
SAME_AS_PID = "events:same_as"
SAME_AS_PROPERTY = KgProperty(
    pid=SAME_AS_PID,
    value_kind="item",
    reva_predicate="same_event_as",
    label_en="same event as (evidence-based link)",
    label_fr="même événement que (lien fondé sur une preuve)",
    inverse_name="same_event_as",
    inverse_label_en="same event as (evidence-based link)",
    inverse_label_fr="même événement que (lien fondé sur une preuve)",
)
MAX_LINKS = 50_000


@dataclass(frozen=True)
class SameAsLink:
    left: str
    right: str
    rule: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def statement_id(self) -> str:
        a, b = sorted((self.left, self.right))
        return f"{self.rule}:{a}={b}"


@dataclass
class SameAsReport:
    links: list[SameAsLink]
    ambiguous: list[dict[str, Any]]
    skipped_rules: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "links": len(self.links),
            "ambiguous": self.ambiguous[:100],
            "ambiguous_count": len(self.ambiguous),
            "skipped_rules": self.skipped_rules,
            "by_rule": {r: sum(1 for x in self.links if x.rule == r) for r in sorted({x.rule for x in self.links})},
        }


def load_rules(path: Path = RULES_PATH) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, Mapping):
        raise ValueError(f"{path}: not a mapping")
    return dict(doc)


def _origin_sql(origin: str | None, alias: str) -> str:
    return "" if origin is None else f" AND {alias}.origin = :origin"


def _id_value(v: Any) -> str | None:
    if isinstance(v, Mapping):
        x = v.get("value")
        return None if x is None else str(x)
    return None


def shared_property_links(
    conn: Connection, rule: Mapping[str, Any], *, origin: str | None
) -> tuple[list[SameAsLink], list[dict[str, Any]]]:
    """Entities sharing a value of one identifier property (``kind: same_property``)."""
    rows = conn.execute(
        text(f"""SELECT s.subject_qid, s.value, s.statement_key FROM kg.statement_current s
                  WHERE s.property_pid = :p AND s.value IS NOT NULL{_origin_sql(origin, "s")}
                  ORDER BY s.subject_qid LIMIT :lim"""),
        {"p": rule["property"], "origin": origin, "lim": MAX_LINKS * 2},
    ).all()
    by_value: dict[str, dict[str, str]] = defaultdict(dict)
    for subj, val, key in rows:
        v = _id_value(val)
        if v is not None:
            by_value[v].setdefault(subj, key)
    links: list[SameAsLink] = []
    ambiguous: list[dict[str, Any]] = []
    max_group = int(rule.get("max_group", 2))
    for v, holders in sorted(by_value.items()):
        if len(holders) < 2:
            continue
        if len(holders) > max_group:
            ambiguous.append({"rule": rule["id"], "value": v, "holders": sorted(holders)})
            continue
        subs = sorted(holders)
        for i, a in enumerate(subs):
            for b in subs[i + 1 :]:
                links.append(
                    SameAsLink(
                        a,
                        b,
                        rule["id"],
                        {
                            "type": "shared_identifier",
                            "property": rule["property"],
                            "value": v,
                            "statement_keys": [holders[a], holders[b]],
                            "rule_note": rule.get("note", ""),
                        },
                    )
                )
    return links, ambiguous


def prefix_property_links(
    conn: Connection, rule: Mapping[str, Any], *, origin: str | None
) -> tuple[list[SameAsLink], list[dict[str, Any]]]:
    """Dataset records ``<id_prefix><key>`` whose ``<key>`` equals the value of a verified identifier property
    stated on another entity (``kind: prefix_vs_property``)."""
    prefix = str(rule["id_prefix"])
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    known = {
        r[0]
        for r in conn.execute(
            text(
                f"""SELECT DISTINCT e.qid FROM kg.entity_current e WHERE e.qid LIKE :like{_origin_sql(origin, "e")}"""
            ),
            {"like": like, "origin": origin},
        ).all()
    }
    rows = conn.execute(
        text(f"""SELECT s.subject_qid, s.value, s.statement_key FROM kg.statement_current s
                  WHERE s.property_pid = :p AND s.value IS NOT NULL{_origin_sql(origin, "s")} LIMIT :lim"""),
        {"p": rule["property"], "origin": origin, "lim": MAX_LINKS * 2},
    ).all()
    by_value: dict[str, dict[str, str]] = defaultdict(dict)
    for subj, val, key in rows:
        v = _id_value(val)
        if v is not None:
            by_value[v].setdefault(subj, key)
    links: list[SameAsLink] = []
    ambiguous: list[dict[str, Any]] = []
    for v, holders in sorted(by_value.items()):
        target = prefix + v
        if target not in known:
            continue
        if len(holders) > 1:
            ambiguous.append({"rule": rule["id"], "value": v, "holders": sorted(holders), "dataset_record": target})
            continue
        ((holder, key),) = holders.items()
        links.append(
            SameAsLink(
                holder,
                target,
                rule["id"],
                {
                    "type": "shared_identifier",
                    "property": rule["property"],
                    "value": v,
                    "dataset_record": target,
                    "statement_keys": [key],
                    "rule_note": rule.get("note", ""),
                },
            )
        )
    return links, ambiguous


def _known(conn: Connection, ids: Sequence[str], origin: str | None) -> set[str]:
    if not ids:
        return set()
    out = {
        r[0]
        for r in conn.execute(
            text(f"SELECT DISTINCT e.qid FROM kg.entity_current e WHERE e.qid = ANY(:q){_origin_sql(origin, 'e')}"),
            {"q": list(ids), "origin": origin},
        ).all()
    }
    out |= {
        r[0]
        for r in conn.execute(
            text(f"""SELECT DISTINCT s.subject_qid FROM kg.statement_current s
                  WHERE s.subject_qid = ANY(:q){_origin_sql(origin, "s")}"""),
            {"q": list(ids), "origin": origin},
        ).all()
    }
    return out


def mapping_table_links(
    conn: Connection, table: Mapping[str, Any], *, origin: str | None, root: Path = REPO_ROOT
) -> tuple[list[SameAsLink], list[dict[str, Any]]]:
    """Links from an evidenced mapping table (``mapping_tables`` entry of the rules): one link per accepted
    entry whose two ids are both known to the KG."""
    path = root / table["path"]
    raw = path.read_bytes()
    doc = json.loads(raw)
    entries = doc.get(table.get("entries_field", "entries")) or []
    accept = set(table.get("accept_status") or ["verified"])
    pairs: list[tuple[str, str, Mapping[str, Any]]] = []
    by_right: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        right = e.get(table["right_field"])
        if e.get("status") not in accept or not isinstance(right, str):
            continue
        left = str(table["left_template"]).format(**e)
        pairs.append((left, right, e))
        by_right[right].append(left)
    known = _known(conn, sorted({x for p in pairs for x in p[:2]}), origin)
    links: list[SameAsLink] = []
    ambiguous: list[dict[str, Any]] = []
    sha = hashlib.sha256(raw).hexdigest()
    for left, right, e in pairs:
        if len(by_right[right]) > 1:
            ambiguous.append({"rule": table["id"], "value": right, "holders": sorted(by_right[right])})
            continue
        if left not in known or right not in known or left == right:
            continue
        links.append(
            SameAsLink(
                left,
                right,
                table["id"],
                {
                    "type": "mapping_table",
                    "table": table["path"],
                    "table_sha256": sha,
                    "table_review_status": doc.get("review_status"),
                    "entry": {
                        k: e.get(k)
                        for k in table.get("evidence_fields", ["status", "basis", "wikidata_revision", "raw_sha256"])
                        if k in e
                    },
                    "entry_key": {k: e.get(k) for k in table.get("key_fields", [])},
                },
            )
        )
    return links, ambiguous


def derive_links(
    conn: Connection, rules: Mapping[str, Any] | None = None, *, origin: str | None = None, root: Path = REPO_ROOT
) -> SameAsReport:
    rules = rules if rules is not None else load_rules()
    links: list[SameAsLink] = []
    ambiguous: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for rule in rules.get("rules") or []:
        if rule.get("status") != "active":
            skipped.append({"rule": rule.get("id"), "status": rule.get("status"), "note": rule.get("note", "")})
            continue
        fn = {"same_property": shared_property_links, "prefix_vs_property": prefix_property_links}[rule["kind"]]
        lk, amb = fn(conn, rule, origin=origin)
        links += lk
        ambiguous += amb
    for table in rules.get("mapping_tables") or []:
        if table.get("status", "active") != "active":
            skipped.append({"rule": table.get("id"), "status": table.get("status"), "note": table.get("note", "")})
            continue
        lk, amb = mapping_table_links(conn, table, origin=origin, root=root)
        links += lk
        ambiguous += amb
    seen: set[str] = set()
    uniq = []
    for x in links:
        if x.statement_id not in seen:
            seen.add(x.statement_id)
            uniq.append(x)
    return SameAsReport(uniq[:MAX_LINKS], ambiguous, skipped)


def evidence_document(links: Iterable[SameAsLink], *, rules_sha256: str) -> bytes:
    body = {
        "schema": "reva-events-same-as/1",
        "method": METHOD,
        "rules_sha256": rules_sha256,
        "links": [
            {"statement_id": x.statement_id, "left": x.left, "right": x.right, "rule": x.rule, "evidence": x.evidence}
            for x in sorted(links, key=lambda x: x.statement_id)
        ],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_links(
    engine: Engine,
    store: Any,
    report: SameAsReport,
    *,
    origin: str,
    rules_sha256: str = "",
    retract_missing: bool = True,
) -> LoadReport:
    """Write the links as ``events:same_as`` statements (worker role, one ``load_batch``); links of earlier runs
    whose evidence is gone are retracted (``removed_at_source``)."""
    register_properties(engine, [SAME_AS_PROPERTY])
    doc = evidence_document(report.links, rules_sha256=rules_sha256)
    raw = store.put_bytes(doc, "raw", "application/json").sha256
    sts = []
    for x in report.links:
        a, b = sorted((x.left, x.right))
        sts.append(
            KgStatement(
                subject_qid=a,
                property_pid=SAME_AS_PID,
                object_qid=b,
                statement_id=x.statement_id,
                raw_sha256=raw,
                qualifiers={
                    "evidence": dict(x.evidence),
                    "rule": x.rule,
                    "derivation": "evidence_linked_identity",
                    "method": METHOD,
                    "symmetric": True,
                },
            )
        )
    retract: list[str] = []
    if retract_missing:
        keep = {f"{SOURCE_KEY}:{x.statement_id}" for x in report.links}
        with engine.connect() as conn:
            cur = [
                r[0]
                for r in conn.execute(
                    text("""SELECT s.statement_key FROM kg.statement_current s
                         WHERE s.source_key = :sk AND s.origin = :o"""),
                    {"sk": SOURCE_KEY, "o": origin},
                ).all()
            ]
        retract = sorted(set(cur) - keep)
    return load_batch(
        engine,
        store,
        statements=sts,
        source_key=SOURCE_KEY,
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin=origin,
        retract_keys=retract,
        parameters={"method": METHOD, "rules_sha256": rules_sha256, "links": len(sts)},
    )


def rules_sha256(path: Path = RULES_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
