"""Generation context: one read-only connection, the artifact store, the pinned atlas snapshot and caches."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from reva.v8.dossier.values import Src

# Human titles of the sources most briefs cite (the source's own name; unknown keys fall back to the rights
# record's provider / attribution, then to the key itself).
SOURCE_TITLES: dict[str, str] = {
    "wb": "Banque mondiale",
    "imf": "FMI",
    "eia": "U.S. EIA — International Energy Data",
    "eia_steo": "U.S. EIA — Short-Term Energy Outlook",
    "eia.opendata_bulk": "U.S. EIA",
    "who": "OMS",
    "ucdp": "UCDP (Uppsala Conflict Data Program)",
    "ucdp.downloads": "UCDP (Uppsala Conflict Data Program)",
    "ucdp.ged": "UCDP GED (événements géoréférencés)",
    "views": "VIEWS (Uppsala / PRIO)",
    "views.api": "VIEWS (Uppsala / PRIO)",
    "un_wpp": "Nations unies — World Population Prospects",
    "owid": "Our World in Data",
    "undp": "PNUD — Rapport sur le développement humain",
    "ilostat": "OIT — ILOSTAT",
    "wikidata.wbapi": "Wikidata",
    "qlever.wikidata": "Wikidata (QLever)",
    "geonames.countryinfo": "GeoNames",
    "chembl.api": "ChEMBL (EMBL-EBI)",
    "who.eml": "OMS — Liste modèle des médicaments essentiels (eEML)",
    "eml.who": "OMS — Liste modèle des médicaments essentiels (eEML)",
    "unichem": "UniChem (EMBL-EBI)",
    "mondo": "Mondo Disease Ontology",
    "doid": "Disease Ontology",
    "wri.gppd": "WRI — Global Power Plant Database",
    "effects.literature": "Publication (estimation attribuée)",
    "reva.forecast_registry": "Registre prospectif REVA",
    "reva.predict": "Moteur de décision REVA",
    "reva.beat": "Évaluation pré-enregistrée REVA",
    "reva.registry": "Registres REVA",
    "pax": "PA-X Peace Agreements Database",
    "ucdppa": "UCDP Peace Agreement Dataset",
    "harvard.dataverse.unvotes": "Votes à l'AGNU (Voeten et al.)",
}


def source_title(key: str, rights: dict[str, Any] | None = None) -> str:
    base = key.split(".")[0]
    if key in SOURCE_TITLES:
        return SOURCE_TITLES[key]
    if base in SOURCE_TITLES:
        return SOURCE_TITLES[base]
    r = rights or {}
    for k in ("provider", "attribution"):
        if isinstance(r.get(k), str) and r[k].strip():
            return str(r[k]).strip()[:120]
    return key


def rights_src(
    source: str,
    rights: dict[str, Any] | None,
    *,
    title: str | None = None,
    dataset: str | None = None,
    indicator: str | None = None,
    period: str | None = None,
    ref: str | None = None,
    url: str | None = None,
    retrieved: str | None = None,
    verification: str | None = None,
    method: str | None = None,
) -> Src:
    r = rights or {}
    lic = r.get("licence")
    return Src(
        source=source,
        title=title or source_title(source, r),
        dataset=dataset,
        indicator=indicator,
        period=period,
        licence=str(lic) if lic else None,
        licence_url=r.get("licence_url") or r.get("terms_url"),
        restricted=bool(r.get("restricted")) or bool(r.get("non_commercial")) or "licence_review" in str(lic or ""),
        non_commercial=bool(r.get("non_commercial")),
        required_notice=r.get("required_notice"),
        ref=ref,
        url=url,
        retrieved=retrieved,
        verification=verification,
        method=method,
    )


class _Lru:
    def __init__(self, size: int) -> None:
        self.size = size
        self.d: OrderedDict[Any, Any] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, k: Any) -> Any:
        with self.lock:
            if k in self.d:
                self.d.move_to_end(k)
                return self.d[k]
        return None

    def put(self, k: Any, v: Any) -> None:
        with self.lock:
            self.d[k] = v
            self.d.move_to_end(k)
            while len(self.d) > self.size:
                self.d.popitem(last=False)


#: Filtered slices of registered atlas tables, per (artifact sha256, filter) — process-wide.
TABLE_SLICES = _Lru(256)
#: Latest version pin per dataset key, per data snapshot id.
TABLE_PINS = _Lru(256)


@dataclass
class Ctx:
    conn: Any
    store: Any
    origin: str = "live"
    snapshot_id: str = ""
    atlas_sha: str | None = None
    atlas_q: Any = None
    subject: dict[str, Any] = field(default_factory=dict)
    memo: dict[Any, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ atlas
    def atlas(self) -> Any:
        if self.atlas_q is None:
            raise LookupError("atlas snapshot not available")
        return self.atlas_q

    def catalog(self) -> Any:
        from reva.atlas.catalog import cached_catalog

        return cached_catalog()

    def headlines(self) -> Any:
        from reva.atlas.source_order import cached_headlines

        return cached_headlines()

    # ------------------------------------------------------------------ registered tables (outside the snapshot)
    def table_pin(self, dataset_key: str) -> dict[str, Any] | None:
        k = (self.snapshot_id, self.origin, dataset_key)
        hit = TABLE_PINS.get(k)
        if hit is not None:
            return hit or None
        from reva.evaluation.registry.data import latest_version

        pin = latest_version(self.conn, dataset_key, origin=self.origin, cutoff=self.now)
        TABLE_PINS.put(k, pin or {})
        return pin

    def table_slice(self, dataset_key: str, sig: tuple[Any, ...], filt: Any) -> tuple[Any, dict[str, Any]] | None:
        """(frame filtered by ``filt``, pin) of the latest registered version of ``dataset_key``."""
        pin = self.table_pin(dataset_key)
        if not pin:
            return None
        k = (pin["artifact_sha256"], sig)
        hit = TABLE_SLICES.get(k)
        if hit is None:
            from reva.atlas.tables import read_atlas_table

            full = self.memo.get(("frame", pin["artifact_sha256"]))
            if full is None:
                full = read_atlas_table(self.store, pin["artifact_sha256"])
                self.memo[("frame", pin["artifact_sha256"])] = full
            hit = filt(full)
            TABLE_SLICES.put(k, hit)
        return hit, pin


def data_snapshot_id(conn: Any, atlas_sha: str | None, origin: str) -> str:
    """Cheap fingerprint of everything a brief reads: the atlas snapshot, the newest registered table version,
    KG / causal / effect loads and the forecast registry batches. Changes whenever any of them changes."""
    row = conn.execute(
        text(
            """SELECT (SELECT max(recorded_at)::text FROM evidence.dataset WHERE origin = :o) AS ds,
                      (SELECT max(id) FROM kg.load) AS kl,
                      (SELECT max(id) FROM kg.causal_load) AS cl,
                      (SELECT max(id) FROM kg.effect_load) AS el,
                      (SELECT max(recorded_at)::text FROM model.evaluation) AS ev"""
        ),
        {"o": origin},
    ).mappings().one()
    blob = json.dumps({"atlas": atlas_sha, "origin": origin, **{k: row[k] for k in row.keys()}}, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
