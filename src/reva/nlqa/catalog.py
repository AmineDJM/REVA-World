"""Allow-lists given to the planner and enforced on its output (trusted, from the registry).

* **Concepts**: ``registry/atlas/catalog/*.yaml`` (ATLASCORE's catalog; concepts are
  ``provider_declared``), or the concepts an atlas adapter reports. The reader is tolerant
  about the exact YAML layout (``concepts:`` list, ``labels: {fr, en}`` or ``label_en``/
  ``label_fr``, ``source_order`` as strings or mappings) but never invents a concept.
* **Countries**: ``registry/entities/country_crosswalk.json`` (ISO 3166-1 alpha-3 + labels).
* **KG properties**: ``registry/kg/properties.yaml`` (WDHARVEST's allow-list).
* **Link kinds**: the interconnection kinds of the V3 spec.

The planner may use only these identifiers; anything else in a plan is rejected and
reported, never substituted.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from reva.nlqa.ports import Concept

REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOG_DIR = REPO_ROOT / "registry" / "atlas" / "catalog"
CROSSWALK = REPO_ROOT / "registry" / "entities" / "country_crosswalk.json"
PROPERTIES = REPO_ROOT / "registry" / "kg" / "properties.yaml"
#: Debian iso-codes (the crosswalk's ISO 3166-1 mirror) and its French translation (review V3b ASK-8)
ISO_CODES = REPO_ROOT / "registry" / "reference" / "iso-codes_4.20.1_iso_3166-1.json"
ISO_CODES_FR = REPO_ROOT / "registry" / "reference" / "iso-codes_4.20.1_iso_3166-1_fr.po"
#: Common names the ISO names and their French translation do not give (curated; matched on whole words)
FR_ALIASES: dict[str, tuple[str, ...]] = {
    "BLR": ("Biélorussie",),
    "CPV": ("Cap-Vert",),
    "GBR": ("Grande-Bretagne",),
    "MKD": ("Macédoine",),
    "MMR": ("Birmanie",),
    "NLD": ("Hollande",),
    "RUS": ("Russia",),
    "SWZ": ("Swaziland",),
    "TLS": ("Timor oriental",),
    "VAT": ("Vatican",),
    "VNM": ("Vietnam",),
}

#: Interconnection kinds (V3 spec, "Interconnections").
LINK_KINDS: tuple[str, ...] = (
    "border",
    "shared_language",
    "colonial",
    "succession",
    "membership",
    "alliance",
    "treaty",
    "conflict",
    "diplomatic",
    "trade",
    "migration",
    "shared_institution",
    "peacekeeping",  # LINKWIRE: UN peacekeeping personnel contributed to a mission in the other country
    "historical_polity",  # LINKWIRE: present territories covered by the same historical polity (overlay)
    "infrastructure",  # LINKS3: submarine cables (Wikidata), air routes (OpenFlights, June 2014), launch-site states (GCAT)
    "sanctions",  # EVMAP: coded sanctions episodes (TIES / EUSANCT) and active OFAC programs (sender -> target)
    "natural_hazard",  # EVMAP: the same tsunami observed in two countries (NCEI; source -> runup country)
)
#: Entity kinds a lookup may ask for (a hint for server-side resolution, never an id).
ENTITY_KINDS: tuple[str, ...] = (
    "country",
    "historical_state",
    "empire",
    "colony",
    "person",
    "event",
    "war",
    "treaty",
    "voyage",
    "organisation",
    "language",
    "religion",
    "era",
    "place",
    "other",
)
#: Concepts shown to the model at most (the most relevant to the question first, then by id).
MAX_PROMPT_CONCEPTS = 160


def fold(text: str) -> str:
    """Casefold + strip accents + collapse spaces (for lexical matching only)."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t.casefold().replace("’", "'")).strip()


_STOPWORDS = frozenset(
    "les des une est sont quel quelle quels quelles comment pourquoi quand dans pour par sur avec entre plus "
    "moins pays ont aux qui que quoi leur leurs son ses cette ces the and for with what which how why when "
    "where who are was were has have had from into than most more less countries country does did per ans "
    "years year annee annees".split()
)


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]{3,}", fold(text)) if w not in _STOPWORDS and not w.isdigit()]


def _stem(word: str) -> str:
    return word[:5]


def _labels(doc: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    labels = doc.get("labels") or doc.get("label")
    if isinstance(labels, Mapping):
        out.update({str(k): str(v) for k, v in labels.items() if v})
    elif isinstance(labels, str):
        out["en"] = labels
    for lang in ("en", "fr"):
        for key in (f"label_{lang}", f"name_{lang}"):
            if doc.get(key):
                out.setdefault(lang, str(doc[key]))
    return out


def _source_order(doc: Mapping[str, Any]) -> tuple[str, ...]:
    raw = doc.get("source_order") or doc.get("sources") or []
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Mapping):
            parts = [str(item[k]) for k in ("source", "dataset", "indicator") if item.get(k)]
            if parts:
                out.append(".".join(parts))
    return tuple(out)


def concepts_from_docs(docs: Iterable[Mapping[str, Any]]) -> list[Concept]:
    out: dict[str, Concept] = {}
    for doc in docs:
        if not isinstance(doc, Mapping):
            continue
        domain = doc.get("domain")
        items = doc.get("concepts")
        if isinstance(items, Mapping):
            items = [{"id": k, **(v if isinstance(v, Mapping) else {})} for k, v in items.items()]
        for c in items or []:
            if not isinstance(c, Mapping):
                continue
            cid = c.get("id") or c.get("concept_id")
            if not isinstance(cid, str) or not CONCEPT_ID_RE.match(cid):
                continue
            definition = c.get("definition")
            if isinstance(definition, Mapping):
                definition = definition.get("en") or definition.get("fr")
            out[cid] = Concept(
                id=cid,
                labels=_labels(c),
                unit=str(c["unit"]) if c.get("unit") is not None else None,
                domain=str(c.get("domain") or domain) if (c.get("domain") or domain) else None,
                definition=str(definition)[:300] if definition else None,
                source_order=_source_order(c),
                review_status=str(c.get("review_status") or doc.get("review_status") or "provider_declared"),
                rankable=c["rankable"] if isinstance(c.get("rankable"), bool) else None,
            )
    return [out[k] for k in sorted(out)]


CONCEPT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


def load_catalog_concepts(directory: Path = CATALOG_DIR) -> list[Concept]:
    if not directory.is_dir():
        return []
    docs = []
    for p in sorted(directory.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        try:
            docs.append(yaml.safe_load(p.read_text()) or {})
        except yaml.YAMLError:
            continue
    return concepts_from_docs(docs)


@dataclass(frozen=True)
class Country:
    iso3: str
    label: str
    names: tuple[str, ...] = ()
    label_fr: str | None = None  # crosswalk label_fr, else the iso-codes French name (ASK-8)


@lru_cache(maxsize=4)
def load_countries(path: Path = CROSSWALK) -> tuple[Country, ...]:
    """Crosswalk countries with their English names (crosswalk label, World Bank, Comtrade, ISO names) and their
    French names (crosswalk ``label_fr``, the iso-codes French translation of the ISO name / common name /
    official name, the short form before the comma when it names one country only, and :data:`FR_ALIASES`)."""
    if not path.exists():
        return ()
    doc = json.loads(path.read_text())
    iso_names, fr = _iso_names(), _iso_fr()
    out = []
    for e in doc.get("entities", []):
        iso3 = e.get("iso3") or e.get("key")
        if not isinstance(iso3, str) or not re.fullmatch(r"[A-Z]{3}", iso3):
            continue
        names = {str(e.get("label") or iso3)}
        for k in ("worldbank_name", "comtrade_reporter_label", "label_fr", "iso_name"):
            if e.get(k):
                names.add(str(e[k]))
        iso = iso_names.get(iso3, {})
        for n in iso.values():
            names.add(n)
            if fr.get(n):
                names.add(fr[n])
        names.update(FR_ALIASES.get(iso3, ()))
        # French display name: the crosswalk's, else the translation of the ISO common name, else of the ISO name
        cands = [fr[x] for k in ("common_name", "name") if (x := iso.get(k)) and fr.get(x)]
        label_fr = e.get("label_fr") or (cands[0] if cands else None)
        out.append(Country(iso3, str(e.get("label") or iso3), tuple(sorted(names)), label_fr))
    # "Russie, Fédération de" -> "Russie": the part before the comma, only when no other country shares it
    heads: dict[str, set[str]] = {}
    for c in out:
        for n in c.names:
            if "," in n:
                heads.setdefault(fold(n.split(",", 1)[0]), set()).add(c.iso3)

    def short(n: str | None, iso3: str) -> str | None:
        if n and "," in n and heads.get(fold(n.split(",", 1)[0])) == {iso3}:
            return n.split(",", 1)[0].strip()
        return n

    return tuple(
        Country(
            c.iso3,
            c.label,
            tuple(sorted(set(c.names) | {x for n in c.names if (x := short(n, c.iso3)) and x != n})),
            short(c.label_fr, c.iso3),
        )
        for c in out
    )


def _iso_names() -> dict[str, dict[str, str]]:
    try:
        rows = json.loads(ISO_CODES.read_text())["3166-1"]
    except (OSError, ValueError, KeyError):
        return {}
    return {
        r["alpha_3"]: {k: r[k] for k in ("name", "common_name", "official_name") if r.get(k)}
        for r in rows
        if r.get("alpha_3")
    }


def _iso_fr() -> dict[str, str]:
    """msgid -> msgstr of the iso-codes French translation (single-line entries; empty translations skipped)."""
    try:
        po = ISO_CODES_FR.read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for m in re.finditer(r'^msgid "((?:[^"\\]|\\.)*)"\nmsgstr "((?:[^"\\]|\\.)*)"$', po, re.M):
        if m.group(1) and m.group(2):
            out[m.group(1).replace('\\"', '"')] = m.group(2).replace('\\"', '"')
    return out


@dataclass(frozen=True)
class KgProperty:
    pid: str
    predicate: str
    kind: str
    labels: Mapping[str, str] = field(default_factory=dict)


@lru_cache(maxsize=4)
def load_properties(path: Path = PROPERTIES) -> tuple[KgProperty, ...]:
    if not path.exists():
        return ()
    doc = yaml.safe_load(path.read_text()) or {}
    out = []
    for p in doc.get("properties") or []:
        pid = p.get("property")
        if not isinstance(pid, str) or not re.fullmatch(r"P[1-9][0-9]{0,6}", pid):
            continue
        labels = {}
        if p.get("label_en") or p.get("expected_en"):
            labels["en"] = str(p.get("label_en") or p.get("expected_en"))
        if p.get("label_fr"):
            labels["fr"] = str(p["label_fr"])
        out.append(KgProperty(pid, str(p.get("reva_predicate") or pid), str(p.get("kind") or "entity"), labels))
    return tuple(out)


@dataclass(frozen=True)
class QaCatalog:
    concepts: Mapping[str, Concept]
    countries: Mapping[str, Country]
    properties: Mapping[str, KgProperty]
    link_kinds: tuple[str, ...] = LINK_KINDS
    concepts_source: str = "registry/atlas/catalog"

    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(json.dumps(sorted(self.concepts), separators=(",", ":")).encode())
        h.update(json.dumps(sorted(self.countries), separators=(",", ":")).encode())
        h.update(json.dumps(sorted(self.properties), separators=(",", ":")).encode())
        h.update(json.dumps(list(self.link_kinds)).encode())
        return h.hexdigest()

    def country_label(self, iso3: str, lang: str = "en") -> str:
        c = self.countries.get(iso3)
        if c is None:
            return iso3
        return (c.label_fr or c.label) if lang == "fr" else c.label

    def relevant_concepts(self, question: str, limit: int = MAX_PROMPT_CONCEPTS) -> list[Concept]:
        """Concepts for the prompt: all when few, else the ``limit`` best lexical matches (deterministic).

        Score = sum of the inverse document frequency of the question's content words found in the
        concept's id/labels; a word matches on its first 5 letters (``densité``/``density``,
        ``chômage``/``chomeur``) so French questions reach English-only labels too. Stop words are ignored.
        This only chooses what the model *sees*; the model may still answer "not in the catalogue"."""
        items = list(self.concepts.values())
        if len(items) <= limit:
            return sorted(items, key=lambda c: c.id)
        docs = {c.id: {_stem(w) for w in _content_words(" ".join([c.id, *c.labels.values()]))} for c in items}
        df: dict[str, int] = {}
        for ws in docs.values():
            for w in ws:
                df[w] = df.get(w, 0) + 1
        n = len(items)
        q = {_stem(w) for w in _content_words(question)}

        def score(c: Concept) -> tuple[float, str]:
            return (-sum(math.log(1 + n / df[w]) for w in q & docs[c.id]), c.id)

        return sorted(items, key=score)[:limit]

    def prompt_view(self, question: str) -> dict[str, Any]:
        concepts = self.relevant_concepts(question)
        return {
            "concepts": [
                {
                    "id": c.id,
                    "en": c.labels.get("en", ""),
                    "fr": c.labels.get("fr", ""),
                    **({"unit": c.unit} if c.unit else {}),
                }
                for c in concepts
            ],
            "concepts_shown": len(concepts),
            "concepts_total": len(self.concepts),
            "kg_properties": [
                {"id": p.pid, "label": p.labels.get("en", p.predicate)}
                for p in sorted(self.properties.values(), key=lambda p: int(p.pid[1:]))
                if p.kind == "entity" or p.pid in ("P571", "P576", "P580", "P582", "P585")
            ],
            "link_kinds": list(self.link_kinds),
            "entity_kinds": list(ENTITY_KINDS),
        }


def _rankable(c: Any) -> bool | None:
    """An explicit ``rankable`` declaration of an atlas catalog concept, when its loader keeps one."""
    v = getattr(c, "rankable", None)
    return v if isinstance(v, bool) else None


def atlas_catalog_concepts() -> list[Concept] | None:
    """Concepts through ATLASCORE's validating loader (:mod:`reva.atlas.catalog`, invalid files skipped),
    or ``None`` when that module is absent."""
    try:
        from reva.atlas.catalog import cached_catalog
    except ImportError:
        return None
    cat = cached_catalog()
    return [
        Concept(
            id=c.id,
            labels=dict(c.labels),
            unit=c.unit,
            domain=c.domain,
            definition=(c.definition or "")[:300] or None,
            source_order=tuple(s.key for s in c.source_order),
            review_status="provider_declared_not_human_reviewed",
            rankable=_rankable(c),
        )
        for c in sorted(cat.concepts.values(), key=lambda c: c.id)
    ]


def build_catalog(
    *,
    concepts: Sequence[Concept] | None = None,
    catalog_dir: Path = CATALOG_DIR,
    crosswalk: Path = CROSSWALK,
    properties: Path = PROPERTIES,
) -> QaCatalog:
    """The allow-lists. Concepts: given, else ATLASCORE's loader for the registry catalog, else the
    tolerant reader of ``catalog_dir`` (test fixtures)."""
    source = "given"
    if concepts is None and catalog_dir == CATALOG_DIR:
        concepts = atlas_catalog_concepts()
        source = "reva.atlas.catalog (registry/atlas/catalog)"
    if concepts is None:
        try:
            source = str(catalog_dir.relative_to(REPO_ROOT))
        except ValueError:
            source = str(catalog_dir)
        concepts = load_catalog_concepts(catalog_dir)
    return QaCatalog(
        concepts={c.id: c for c in concepts},
        countries={c.iso3: c for c in load_countries(crosswalk)},
        properties={p.pid: p for p in load_properties(properties)},
        concepts_source=source,
    )
