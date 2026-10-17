"""Build the V8 search index (``uv run reva v8 index build``).

Reads the dev DB (read-only, ``reva_api`` role) and the registries, writes one SQLite file atomically:

* ``doc``  — one row per searchable record (kind, id, labels fr/en, French sublabel, href, group, prior, extra);
* ``name`` — every label / alias (normalised text) and every identifier / code (normalised code) of a record;
* ``name_fts`` — FTS5 trigram index over the text names (substring and word matches);
* ``meta`` — build time, version, counts per collector, collector errors (a failed collector is reported, the
  others still build).

Search is lookup, not identity: each record is one source's record. The only merge is declared by code
(``registry/entities/country_qids.json``: ISO3 → Wikidata QID), never by name.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from reva.v8.search import hrefs
from reva.v8.search.normalize import norm_code, norm_text

INDEX_VERSION = "v8s-1"
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DIR = REPO_ROOT / "var" / "v8" / "search_index"
INDEX_FILE = "search.sqlite"
ENV_DIR = "REVA_V8_SEARCH_DIR"
POP_PRIOR = 0.42  # docs at or above this prior are kept in memory for prefix / fuzzy ranking
MAX_NAME = 300

GROUPS = {
    "country": "Pays & régions",
    "region": "Pays & régions",
    "indicator": "Indicateurs",
    "table": "Tables",
    "entity": "Entités",
    "question": "Prévisions",
    "family": "Prévisions",
    "decision": "Décisions",
    "effect": "Effets",
    "source": "Sources",
}
GROUP_ORDER = list(dict.fromkeys(GROUPS.values()))
SEARCH_KINDS = tuple(GROUPS)
REGION_KINDS = {"region_adm1", "nuts_region", "oecd_region"}
LOW_VALUE_KINDS = {
    "weather_station",
    "disaster_displacement",
    "disaster_event",
    "un_roll_call",
    "hs_code",
    "cpc_code",
    "sitc_code",
    "isotope",
    "genome_assembly",
    "survey_wave",
    "coded_outcome",
    "dispute_outcome",
    "crisis_outcome",
    "crisis_response",
    "crisis_trigger",
    "war_outcome",
    "campaign_outcome",
    "conflict_termination",
    "polity_relation",
    "place_name",
}
KIND_FR = {
    "country": "Pays",
    "historical_state": "État historique",
    "taxon": "Taxon",
    "facility": "Installation",
    "gene": "Gène",
    "protein": "Protéine",
    "event": "Événement",
    "disease": "Maladie",
    "disease_or_phenotype": "Maladie ou phénotype",
    "phenotype": "Phénotype",
    "wikidata_item": "Élément Wikidata",
    "weather_station": "Station météo",
    "disaster_event": "Catastrophe",
    "disaster_displacement": "Déplacement (catastrophe)",
    "hs_code": "Code SH",
    "dialect": "Dialecte",
    "language": "Langue",
    "anatomical_structure": "Structure anatomique",
    "earthquake": "Séisme",
    "un_roll_call": "Vote AGNU",
    "airport": "Aéroport",
    "isotope": "Isotope",
    "drug": "Médicament",
    "drug_substance": "Substance",
    "compound": "Composé chimique",
    "family": "Famille",
    "cpc_code": "Code CPC",
    "research_topic": "Thème de recherche",
    "militarized_incident": "Incident militarisé",
    "region_adm1": "Région (ISO 3166-2)",
    "nuts_region": "Région NUTS",
    "oecd_region": "Région OCDE",
    "sitc_code": "Code CTCI",
    "person": "Personne",
    "peace_agreement": "Accord de paix",
    "legal_entity": "Entité juridique (LEI)",
    "militarized_interstate_dispute": "Différend interétatique militarisé",
    "armed_group": "Groupe armé",
    "armed_conflict": "Conflit armé",
    "sanction_episode": "Épisode de sanctions",
    "polity": "Entité politique",
    "cell_type": "Type cellulaire",
    "territory": "Territoire",
    "heritage_site": "Site du patrimoine mondial",
    "foreign_policy_crisis": "Crise de politique étrangère",
    "international_crisis": "Crise internationale",
    "mesh_descriptor": "Descripteur MeSH",
    "volcanic_eruption": "Éruption volcanique",
    "tsunami": "Tsunami",
    "symptom": "Symptôme",
    "government_entity": "Entité gouvernementale",
    "drug_target": "Cible thérapeutique",
    "intangible_heritage_element": "Patrimoine immatériel",
    "treaty": "Traité",
    "commodity": "Produit de base",
    "occupation": "Profession",
    "chemical_element": "Élément chimique",
    "physical_constant": "Constante physique",
    "climate_index": "Indice climatique",
    "atlas_table": "Table de l'atlas",
    "organization": "Organisation",
    "un_peace_operation": "Opération de paix ONU",
    "ecoregion": "Écorégion",
    "food_material": "Aliment",
    "state_code": "Code d'État",
    "sanction_program": "Programme de sanctions",
    "sanction_regime": "Régime de sanctions",
    "energy_source": "Source d'énergie",
    "unclassified": "Non classé",
}


def kind_fr(kind: str | None) -> str:
    if not kind:
        return "Entité"
    return KIND_FR.get(kind) or kind.replace("_", " ").capitalize()


def index_dir() -> Path:
    return Path(os.environ.get(ENV_DIR) or DEFAULT_DIR)


def index_path(directory: Path | None = None) -> Path:
    return (directory or index_dir()) / INDEX_FILE


@dataclass
class Doc:
    kind: str
    id: str
    label_fr: str | None
    label_en: str | None
    sublabel: str
    href: str
    entity_kind: str | None = None
    source: str | None = None
    origin: str = "live"
    prior: float = 0.3
    extra: dict[str, Any] = field(default_factory=dict)
    compact: bool = False  # KG record: sublabel holds the description only, href/extra rebuilt at query time
    texts: list[str] = field(default_factory=list)  # labels / aliases (searched as text)
    codes: list[str] = field(default_factory=list)  # identifiers / codes (searched exactly)

    @property
    def group(self) -> str:
        return GROUPS[self.kind]


def _short(s: str | None, n: int = 110) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------------------------------------ writer
SCHEMA = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE strs (sid INTEGER PRIMARY KEY, s TEXT NOT NULL UNIQUE);
CREATE TABLE doc (
  rid INTEGER PRIMARY KEY, kind INTEGER NOT NULL, id TEXT NOT NULL, label_fr TEXT, label_en TEXT,
  sublabel TEXT, href TEXT, ek INTEGER, src INTEGER, origin INTEGER NOT NULL, prior REAL NOT NULL, extra TEXT
);
CREATE TABLE name (nid INTEGER PRIMARY KEY, rid INTEGER NOT NULL, norm TEXT NOT NULL, raw TEXT,
  pop INTEGER NOT NULL);
CREATE TABLE code (norm TEXT NOT NULL, rid INTEGER NOT NULL, raw TEXT, PRIMARY KEY (norm, rid)) WITHOUT ROWID;
"""
ORIGINS = ("live", "test_fixture")


class IndexWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        self.tmp.parent.mkdir(parents=True, exist_ok=True)
        if self.tmp.exists():
            self.tmp.unlink()
        self.db = sqlite3.connect(self.tmp)
        self.db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=4096;" + SCHEMA)
        self.rid = 0
        self.nid = 0
        self.counts: dict[str, int] = {}
        self._docs: list[tuple[Any, ...]] = []
        self._names: list[tuple[Any, ...]] = []
        self._codes: list[tuple[Any, ...]] = []
        self.strs: dict[str, int] = {}

    def sid(self, v: str | None) -> int | None:
        if v is None:
            return None
        if v not in self.strs:
            self.strs[v] = len(self.strs) + 1
        return self.strs[v]

    def add(self, d: Doc) -> None:
        self.rid += 1
        pop = 1 if d.prior >= POP_PRIOR else 0
        label_fr = None if d.compact and d.label_fr == d.label_en else d.label_fr
        self._docs.append(
            (
                self.rid,
                SEARCH_KINDS.index(d.kind),
                d.id,
                label_fr,
                d.label_en,
                d.sublabel or None,
                None if d.compact else d.href,
                self.sid(d.entity_kind),
                self.sid(d.source),
                ORIGINS.index(d.origin),
                round(d.prior, 4),
                None if d.compact or not d.extra else json.dumps(d.extra, ensure_ascii=False, separators=(",", ":")),
            )
        )
        seen: set[tuple[str, int]] = set()
        for raw in [d.label_fr, d.label_en, *d.texts]:
            if not raw:
                continue
            n = norm_text(raw)[:MAX_NAME]
            if n and (n, 0) not in seen:
                seen.add((n, 0))
                self.nid += 1
                self._names.append((self.nid, self.rid, n, raw[:MAX_NAME] if pop else None, pop))
        for raw in d.codes:
            n = norm_code(raw)[:MAX_NAME] if raw else ""
            if n and (n, 1) not in seen:
                seen.add((n, 1))
                self._codes.append((n, self.rid, None if raw.lower() == n else raw[:MAX_NAME]))
        self.counts[d.kind] = self.counts.get(d.kind, 0) + 1
        if len(self._docs) >= 20000:
            self.flush()

    def flush(self) -> None:
        self.db.executemany("INSERT INTO doc VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", self._docs)
        self.db.executemany("INSERT INTO name VALUES (?,?,?,?,?)", self._names)
        self.db.executemany("INSERT OR IGNORE INTO code VALUES (?,?,?)", self._codes)
        self._docs.clear()
        self._names.clear()
        self._codes.clear()

    def finish(self, meta: dict[str, Any]) -> Path:
        self.flush()
        db = self.db
        db.executescript(
            """
            CREATE INDEX name_norm ON name(norm);
            CREATE VIRTUAL TABLE name_fts USING fts5(norm, content='', tokenize='trigram', detail='full');
            INSERT INTO name_fts(rowid, norm) SELECT nid, norm FROM name;
            INSERT INTO name_fts(name_fts) VALUES ('optimize');
            """
        )
        db.executemany("INSERT INTO strs VALUES (?, ?)", [(i, v) for v, i in self.strs.items()])
        write_pop_layer(db)
        codes = db.execute("SELECT count(*) FROM code").fetchone()[0]
        meta = {**meta, "documents": self.rid, "names": self.nid, "codes": codes, "kinds": self.counts}
        db.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v, default=str)) for k, v in meta.items()])
        db.commit()
        db.execute("VACUUM")
        db.close()
        os.replace(self.tmp, self.path)
        return self.path

    def abort(self) -> None:
        try:
            self.db.close()
        finally:
            if self.tmp.exists():
                self.tmp.unlink()


def write_pop_layer(db: sqlite3.Connection) -> int:
    """Persist the in-memory layer of popular names (sorted norms, priors, trigram postings) as blobs."""
    import numpy as np

    from reva.v8.search.normalize import trigrams

    rows = db.execute(
        "SELECT n.rid, n.norm, COALESCE(n.raw, n.norm), d.prior, d.kind FROM name n JOIN doc d ON d.rid = n.rid "
        "WHERE n.pop = 1 ORDER BY n.norm"
    ).fetchall()
    post: dict[str, list[int]] = {}
    ntri = np.zeros(len(rows), dtype=np.int32)
    for i, r in enumerate(rows):
        tg = trigrams(r[1])
        ntri[i] = len(tg)
        for t in tg:
            post.setdefault(t, []).append(i)
    blobs = {
        "rid": np.array([r[0] for r in rows], dtype=np.int64).tobytes(),
        "prior": np.array([r[3] for r in rows], dtype=np.float32).tobytes(),
        "kind": np.array([r[4] for r in rows], dtype=np.int8).tobytes(),
        "ntri": ntri.tobytes(),
        "norm": "\n".join(r[1] for r in rows).encode(),
        "raw": "\n".join(" ".join(r[2].split()) for r in rows).encode(),
    }
    db.execute("CREATE TABLE pop (k TEXT PRIMARY KEY, v BLOB NOT NULL)")
    db.executemany("INSERT INTO pop VALUES (?, ?)", list(blobs.items()))
    db.execute("CREATE TABLE pop_tri (tri TEXT PRIMARY KEY, idx BLOB NOT NULL) WITHOUT ROWID")
    db.executemany(
        "INSERT INTO pop_tri VALUES (?, ?)", [(t, np.array(v, dtype=np.int32).tobytes()) for t, v in post.items()]
    )
    return len(rows)


def _is_code_like(s: str) -> bool:
    """A provider indicator code worth an exact-id match (``NY.GDP.MKTP.CD``, ``PCPIPCH``), not a plain word."""
    return len(s) >= 4 and "|" not in s and (any(not c.isalpha() for c in s) or s.isupper())


# ------------------------------------------------------------------------------------------------ collectors
def _country_registry() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    from reva.atlas.tables import CROSSWALK_PATH

    cw = json.loads(Path(CROSSWALK_PATH).read_text(encoding="utf-8"))
    qids_path = REPO_ROOT / "registry" / "entities" / "country_qids.json"
    qids = json.loads(qids_path.read_text(encoding="utf-8")).get("countries", {}) if qids_path.exists() else {}
    return list(cw["entities"]), qids


def countries(conn: Connection, origin: str | None) -> Iterator[Doc]:
    """Atlas crosswalk entities (ISO3), each merged with its Wikidata record by the declared ISO3 → QID map."""
    ents, qids = _country_registry()
    kg_names = _kg_names(conn, [q["qid"] for q in qids.values() if q.get("qid")], origin)
    for e in ents:
        key = e["key"]
        q = qids.get(key) or {}
        qid = q.get("qid")
        label_fr = e.get("label_fr") or q.get("label_fr")
        label_en = e.get("label") or q.get("label_en")
        texts = [e.get("iso_name"), e.get("un_name"), e.get("worldbank_name"), q.get("crosswalk_label")]
        texts += kg_names.get(qid, []) if qid else []
        codes = [key, e.get("iso3"), e.get("iso2"), e.get("iso_numeric"), e.get("m49")]
        codes += [a["key"] for a in e.get("aliases", []) if a.get("provider") in ("iso3166_alpha3", "worldbank")]
        if qid:
            codes.append(qid)
        sovereign = e.get("in_iso3166_1") and e.get("kind") == "entity.country"
        codes_txt = " · ".join(c for c in (e.get("iso3") or key, qid) if c)
        region = e.get("m49_subregion") or e.get("m49_region")
        yield Doc(
            kind="country",
            id=key,
            label_fr=label_fr,
            label_en=label_en,
            sublabel=" · ".join(x for x in ("Pays" if sovereign else "Territoire / agrégat", codes_txt, region) if x),
            href=hrefs.entity("country", key),
            entity_kind="country",
            source="reva.country_crosswalk",
            prior=1.0 if sovereign else 0.6,
            extra={k: v for k, v in {"iso3": e.get("iso3"), "iso2": e.get("iso2"), "qid": qid}.items() if v},
            texts=[t for t in texts if t],
            codes=[c for c in codes if c],
        )


def _kg_names(conn: Connection, qids: list[str], origin: str | None) -> dict[str, list[str]]:
    if not qids:
        return {}
    oc = " AND l.origin = :o" if origin else ""
    rows = conn.execute(
        text(
            f"""SELECT l.qid, l.label FROM kg.entity_label l JOIN kg.entity_current e ON e.id = l.entity_id
                WHERE l.qid = ANY(:q) AND l.lang IN ('fr', 'en'){oc}"""
        ),
        {"q": qids, **({"o": origin} if origin else {})},
    )
    out: dict[str, list[str]] = {}
    for qid, label in rows:
        out.setdefault(qid, []).append(label)
    return out


ID_PROPERTIES = (
    "chembl:atc_code",
    "doid:xref",
    "mondo:xref",
    "uberon:xref",
    "eml:drugbank_id",
    "unichem:drugbank_id",
    "glottolog:iso639_3",
    "hgnc:ensembl_gene_id",
    "isco:code",
    "ourairports:iata",
    "ourairports:icao",
    "ourairports:ident",
    "pubchem:inchikey",
    "P297",
    "P298",
    "P299",
    "P218",
    "P219",
    "P220",
    "P498",
    "P1394",
)


def _id_codes(conn: Connection, origin: str | None) -> dict[str, list[str]]:
    """subject → external identifiers published by the record's own source (lookup only, never identity)."""
    oc = " AND origin = :o" if origin else ""
    rows = conn.execute(
        text(
            f"""SELECT subject_qid, property_pid, value->>'value' FROM kg.statement_current
                WHERE property_pid = ANY(:p) AND value IS NOT NULL{oc}"""
        ),
        {"p": list(ID_PROPERTIES), **({"o": origin} if origin else {})},
    )
    out: dict[str, list[str]] = {}
    for subj, pid, val in rows:
        if val and len(val) <= 100:
            out.setdefault(subj, []).append(val)
            if pid == "hgnc:ncbi_gene_id":
                out[subj].append(f"NCBIGene:{val}")
    return out


def _qid_codes(qid: str) -> list[str]:
    """The record's own id plus readable variants (``mondo:0005015`` → ``MONDO:0005015``, local part)."""
    out = [qid]
    if ":" in qid:
        prefix, local = qid.split(":", 1)
        if any(c.isalpha() for c in local) and any(c.isdigit() for c in local) and len(local) >= 4:
            out.append(local)  # CHEMBL941, P04637, IGJSJL3JD5P30I6NJZ34 …
        if prefix in ("mondo", "doid", "uberon", "cl", "hp", "efo", "go", "so", "symp", "trans", "geno", "foodon"):
            out.append(f"{prefix.upper()}_{local}")
    return out


def _entity_prior(kind: str, sitelinks: int | None) -> float:
    s = min(1.0, math.log1p(sitelinks or 0) / math.log1p(300))
    p = 0.12 + 0.55 * s
    if kind in LOW_VALUE_KINDS:
        p *= 0.5
    if kind in ("drug", "disease", "gene", "commodity", "international_crisis", "armed_conflict", "treaty"):
        p += 0.08
    return min(p, 0.9)


def kg_entities(conn: Connection, origin: str | None, skip: set[str]) -> Iterator[Doc]:
    """Every current KG record (fr/en labels and aliases, own id, published identifiers)."""
    ids = _id_codes(conn, origin)
    oc = "WHERE e.origin = :o" if origin else ""
    sql = text(
        f"""SELECT e.qid, e.kind, e.sitelinks, e.source_key, e.origin, e.descriptions->>'fr' AS d_fr,
                   e.descriptions->>'en' AS d_en, l.lang, l.is_alias, l.label
            FROM kg.entity_current e
            LEFT JOIN kg.entity_label l ON l.entity_id = e.id AND l.lang IN ('fr', 'en')
            {oc}
            ORDER BY e.qid, e.origin"""
    )
    cur: dict[str, Any] | None = None

    def emit(c: dict[str, Any]) -> Doc:
        kind = c["kind"]
        qid = c["qid"]
        label_fr, label_en = c["fr"], c["en"]
        desc = c["d_fr"] or c["d_en"]
        search_kind = "region" if kind in REGION_KINDS else "entity"
        return Doc(
            kind=search_kind,
            id=qid,
            label_fr=label_fr,
            label_en=label_en,
            sublabel=_short(desc, 56) if desc else "",
            href="",
            entity_kind=kind,
            source=c["source_key"],
            origin=c["origin"],
            prior=_entity_prior(kind, c["sitelinks"]) + (0.1 if search_kind == "region" else 0.0),
            compact=True,
            texts=c["aliases"],
            codes=_qid_codes(qid) + ids.get(qid, []),
        )

    result = conn.execute(sql.execution_options(stream_results=True, yield_per=20000), {"o": origin} if origin else {})
    for r in result:
        if r.qid in skip:
            continue
        if cur is None or cur["qid"] != r.qid or cur["origin"] != r.origin:
            if cur is not None:
                yield emit(cur)
            cur = {
                "qid": r.qid,
                "kind": r.kind,
                "sitelinks": r.sitelinks,
                "source_key": r.source_key,
                "origin": r.origin,
                "d_fr": r.d_fr,
                "d_en": r.d_en,
                "fr": None,
                "en": None,
                "aliases": [],
            }
        if r.label is None:
            continue
        if not r.is_alias and cur[r.lang] is None:
            cur[r.lang] = r.label
        else:
            cur["aliases"].append(r.label)
    if cur is not None:
        yield emit(cur)


def atlas_indicators(conn: Connection, origin: str | None) -> Iterator[Doc]:
    from reva.atlas.catalog import cached_catalog
    from reva.atlas.source_order import cached_headlines

    for h in cached_headlines().concepts.values():
        first = h.series[0].as_dict()["series"] if h.series else ""
        table = ".".join(first.split(".")[:2]) if first else "headline"
        yield Doc(
            kind="indicator",
            id=h.id,
            label_fr=h.labels.get("fr"),
            label_en=h.labels.get("en"),
            sublabel=" · ".join(x for x in ("Indicateur phare (meilleure source)", h.unit, h.theme) if x),
            href=hrefs.series(table, {"concept": h.id, "headline": "1"}),
            source="reva.headline",
            prior=0.8,
            extra={"unit": h.unit, "headline": True, "theme": h.theme},
            codes=[h.id],
        )
    for c in cached_catalog().concepts.values():
        first = c.source_order[0] if c.source_order else None
        table = f"{first.source}.{first.dataset}" if first else c.domain
        params = {"concept": c.id}
        if first:
            params["indicator"] = first.indicator
        codes = [c.id] + [s.indicator for s in c.source_order if _is_code_like(s.indicator)]
        yield Doc(
            kind="indicator",
            id=c.id,
            label_fr=c.labels.get("fr"),
            label_en=c.labels.get("en"),
            sublabel=" · ".join(
                x
                for x in ("Indicateur", c.unit, f"{c.domain}" + (f"/{c.subdomain}" if c.subdomain else ""), table)
                if x
            ),
            href=hrefs.series(table, params),
            source=first.source if first else None,
            prior=0.5,
            extra={"unit": c.unit, "domain": c.domain, "table": table},
            texts=list(c.tags),
            codes=codes,
        )


def atlas_tables(conn: Connection, origin: str | None) -> Iterator[Doc]:
    import re

    oc = " AND origin = :o" if origin else ""
    rows = conn.execute(
        text(
            f"""SELECT DISTINCT ON (subject_key) subject_key, dataset_key, row_count, coverage, origin, recorded_at
                FROM evidence.dataset WHERE concept_id = 'atlas.table'{oc}
                ORDER BY subject_key, recorded_at DESC"""
        ),
        {"o": origin} if origin else {},
    ).mappings()
    quoted = re.compile(r"'([^']{4,160})'")
    for r in rows:
        cov = r["coverage"] or {}
        note = cov.get("coverage_note") or ""
        m = quoted.search(note)
        title = m.group(1) if m else _short(note, 90) or r["subject_key"]
        period = (
            f"{str(cov.get('period_min', ''))[:4]}–{str(cov.get('period_max', ''))[:4]}"
            if cov.get("period_min")
            else ""
        )
        yield Doc(
            kind="table",
            id=r["subject_key"],
            label_fr=None,
            label_en=f"{title} ({r['subject_key']})",
            sublabel=" · ".join(
                x
                for x in (
                    "Table de l'atlas",
                    r["subject_key"],
                    f"{r['row_count']:,} lignes".replace(",", " "),
                    f"{cov['countries']} pays" if cov.get("countries") else "",
                    period,
                )
                if x
            ),
            href=hrefs.series(r["subject_key"]),
            source=cov.get("source") or r["subject_key"].split(".")[0],
            origin=r["origin"],
            prior=0.45,
            extra={"rows": r["row_count"], "recorded_at": r["recorded_at"].isoformat()},
            texts=[title],
            codes=[r["subject_key"], r["dataset_key"]],
        )


def forecasts(conn: Connection, origin: str | None, store: Any) -> Iterator[Doc]:
    from reva.evaluation.registry import read as R

    for f in R.families(conn, origin):
        yield Doc(
            kind="family",
            id=f["key"],
            label_fr=f["label_fr"],
            label_en=f["label_en"],
            sublabel=" · ".join(
                x for x in ("Famille de questions de prévision", f.get("unit"), f"statut : {f['status']}") if x
            ),
            href=hrefs.scoreboard(f["key"]),
            source="reva.forecast_registry",
            prior=0.6,
            extra={"status": f["status"], "concept_id": f.get("concept_id")},
            codes=[f["key"]],
        )
    if store is None:
        return
    rows, _ = R.questions(conn, store, origin, limit=10**7)
    status_fr = {
        "pending": "en attente",
        "resolved": "résolue",
        "void": "annulée",
        "not_prospective": "non prospective",
    }
    for q in rows:
        st = q["status"]
        yield Doc(
            kind="question",
            id=q["question_id"],
            label_fr=q["text_fr"] or q["label_fr"],
            label_en=q["text_en"] or q["label_en"],
            sublabel=" · ".join(
                x
                for x in (
                    "Question de prévision",
                    q.get("geo_label_fr") or q.get("geo"),
                    str(q.get("reference_period") or ""),
                    status_fr.get(st, st),
                )
                if x
            ),
            href=hrefs.entity("question", q["question_id"]),
            source="reva.forecast_registry",
            prior=0.42 if st == "pending" else 0.36,
            extra={"status": st, "family": q["family"], "geo": q["geo"], "cycle": q["cycle"]},
            texts=[q["label_fr"], q["label_en"]],
            codes=[q["question_id"], q["question_key"]],
        )


def decisions(conn: Connection, origin: str | None) -> Iterator[Doc]:
    from reva.predict.decision import reference_decisions

    for d in reference_decisions().values():
        j = d.model_dump(mode="json")
        yield Doc(
            kind="decision",
            id=j["id"],
            label_fr=j.get("label_fr"),
            label_en=j.get("label_en"),
            sublabel=" · ".join(
                x for x in ("Décision de référence (scénario non validé)", j.get("instrument"), j.get("start")) if x
            ),
            href=hrefs.decision(j["id"]),
            source="reva.predict",
            prior=0.65,
            extra={"instrument": j.get("instrument")},
            codes=[j["id"]],
        )


def effects(conn: Connection, origin: str | None) -> Iterator[Doc]:
    from reva.effects import query as eq

    after: int | None = None
    org = origin or "any"
    while True:
        rows = eq._rows(conn, origin=org, limit=eq.MAX_LIMIT, after_id=after, res=eq.default_resolver())
        if not rows:
            return
        res = eq.default_resolver()
        for r in rows:
            j = eq.estimate_json(r, res)
            c, e = j["cause"], j["effect"]
            who = eq._who(r)[1]
            yield Doc(
                kind="effect",
                id=r["estimate_key"],
                label_fr=f"{c['label_fr']} → {e['label_fr']}",
                label_en=f"{c['label']} → {e['label']}",
                sublabel=" · ".join(
                    x for x in ("Estimation d'effet attribuée", r["measure"], r["method"], _short(who, 60)) if x
                ),
                href=hrefs.entity("effect", r["estimate_key"]),
                source=r["source_key"],
                origin=r["origin"],
                prior=0.45,
                extra={"measure": r["measure"], "method": r["method"], "source_kind": r["source_kind"]},
                texts=[c["label_fr"], c["label"], e["label_fr"], e["label"]],
                codes=[r["estimate_key"]],
            )
        after = rows[-1]["id"]


def sources(conn: Connection, origin: str | None) -> Iterator[Doc]:
    import yaml

    from reva.atlas.sources import atlas_sources

    seen: set[str] = set()
    titles: dict[str, tuple[str, str | None]] = {}
    for p in sorted((REPO_ROOT / "registry" / "sources").glob("*.yaml")):
        try:
            y = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: S112 - one bad file never breaks the index
            continue
        if isinstance(y, dict) and y.get("source_key"):
            lic = (y.get("rights") or {}).get("licence") if isinstance(y.get("rights"), dict) else None
            titles[str(y["source_key"])] = (str(y.get("title") or y["source_key"]), lic)
    atlas_short: dict[str, str] = {}
    for entry in atlas_sources().values():
        sp = entry.spec
        atlas_short.setdefault(sp.source_key, sp.atlas_source)
        if sp.source_key not in titles:
            titles[sp.source_key] = (sp.provider, sp.licence)
    kg_keys = [r[0] for r in conn.execute(text("SELECT DISTINCT source_key FROM kg.load"))]
    for k in kg_keys:
        titles.setdefault(k, (k, None))
    for key, (title, lic) in sorted(titles.items()):
        if key in seen:
            continue
        seen.add(key)
        codes = [key]
        if key in atlas_short:
            codes.append(atlas_short[key])
        yield Doc(
            kind="source",
            id=key,
            label_fr=None,
            label_en=_short(title, 140),
            sublabel=" · ".join(x for x in ("Source", key, _short(lic, 60) if lic else "") if x),
            href=hrefs.entity("source", key),
            source=key,
            prior=0.45,
            extra={"licence": lic} if lic else {},
            texts=[title],
            codes=codes,
        )


# ------------------------------------------------------------------------------------------------ build
def build_index(
    conn: Connection,
    *,
    out_dir: Path | None = None,
    origin: str | None = None,
    store: Any = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Collect every document and write the index atomically. Returns the build report (meta)."""
    t0 = time.perf_counter()
    path = index_path(out_dir)
    w = IndexWriter(path)
    errors: list[dict[str, str]] = []
    timings: dict[str, float] = {}
    conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))  # tiny /dev/shm in the dev container
    conn.execute(text("SET LOCAL statement_timeout = 0"))
    merged_qids: set[str] = set()

    def run(name: str, docs: Callable[[], Iterable[Doc]]) -> None:
        t = time.perf_counter()
        n = 0
        try:
            for d in docs():
                w.add(d)
                n += 1
                if d.kind == "country" and d.extra.get("qid"):
                    merged_qids.add(d.extra["qid"])
        except Exception as e:  # a failed collector is reported, the others still build
            errors.append({"collector": name, "error": f"{type(e).__name__}: {str(e)[:300]}"})
            conn.execute(text("ROLLBACK TO SAVEPOINT v8_build"))
        else:
            conn.execute(text("RELEASE SAVEPOINT v8_build"))
        conn.execute(text("SAVEPOINT v8_build"))
        timings[name] = round(time.perf_counter() - t, 2)
        if log:
            log(f"{name}: {n} documents in {timings[name]} s")

    try:
        conn.execute(text("SAVEPOINT v8_build"))
        run("countries", lambda: countries(conn, origin))
        run("atlas_indicators", lambda: atlas_indicators(conn, origin))
        run("atlas_tables", lambda: atlas_tables(conn, origin))
        run("forecasts", lambda: forecasts(conn, origin, store))
        run("decisions", lambda: decisions(conn, origin))
        run("effects", lambda: effects(conn, origin))
        run("sources", lambda: sources(conn, origin))
        run("kg_entities", lambda: kg_entities(conn, origin, merged_qids))
        t = time.perf_counter()
        try:
            write_load_counts(conn, path.parent)
            conn.execute(text("RELEASE SAVEPOINT v8_build"))
        except Exception as e:  # reported like a failed collector
            errors.append({"collector": "load_counts", "error": f"{type(e).__name__}: {str(e)[:300]}"})
            conn.execute(text("ROLLBACK TO SAVEPOINT v8_build"))
        timings["load_counts"] = round(time.perf_counter() - t, 2)
        meta = {
            "version": INDEX_VERSION,
            "built_at": datetime.now(UTC).isoformat(),
            "origin": origin or "any",
            "errors": errors,
            "timings_s": timings,
            "pop_prior": POP_PRIOR,
        }
        w.finish(meta)
    except BaseException:
        w.abort()
        raise
    meta["documents"] = w.rid
    meta["names"] = w.nid
    meta["kinds"] = w.counts
    meta["path"] = str(path)
    meta["bytes"] = path.stat().st_size
    meta["seconds"] = round(time.perf_counter() - t0, 1)
    return meta


def write_load_counts(conn: Connection, directory: Path) -> dict[str, Any]:
    """Rows per KG load and per causal load (immutable, append-only tables without a load index): one scan here
    so that the change feed reports counts without scanning per request. Written next to the index."""
    kg: dict[str, dict[str, int]] = {}
    for lid, n in conn.execute(text("SELECT load_id, count(*) FROM kg.entity GROUP BY load_id")):
        kg.setdefault(str(lid), {})["entities"] = int(n)
    for lid, n in conn.execute(text("SELECT load_id, count(*) FROM kg.statement GROUP BY load_id")):
        kg.setdefault(str(lid), {})["statements"] = int(n)
    for v in kg.values():
        v.setdefault("entities", 0)
        v.setdefault("statements", 0)
    causal = {
        str(lid): int(n)
        for lid, n in conn.execute(text("SELECT load_id, count(*) FROM kg.causal_claim GROUP BY load_id"))
    }
    doc = {"computed_at": datetime.now(UTC).isoformat(), "kg": kg, "causal": causal}
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"load_counts.json.tmp{os.getpid()}"
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, directory / "load_counts.json")
    return doc
