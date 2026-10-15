"""Natural Earth 1:110m Admin-0 countries (template naturalearth_asset).

Display geography only. The asset is pinned by SHA-256; download uses
conditional requests (ETag/Last-Modified) and only re-publishes on change.
Countries are matched through reviewed ADM0_A3 aliases; unmatched, disputed and
overseas parts are recorded explicitly.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from typing import Any

import shapefile  # pyshp

from reva.adapters.connectors.base import module_digest, schema_fingerprint, to_raw
from reva.adapters.http.archive import read_zip_members
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import (
    ConnectorSpec,
    Cursor,
    FetchBudget,
    FetchItem,
    FetchOutcome,
    FetchPlan,
    Finding,
    ObservationBatch,
    QualityReport,
    RawAcquisition,
)

ASSET_URL = "https://naciscdn.org/naturalearth/110m/cultural/ne_110m_admin_0_countries.zip"
KEEP_PROPS = ("ADM0_A3", "ISO_A3", "ISO_A3_EH", "NAME", "NAME_LONG", "SOVEREIGNT", "SOV_A3", "TYPE",
              "CONTINENT", "REGION_UN", "SUBREGION", "NOTE_ADM0", "NOTE_BRK")
DISPUTED_TYPES = {"Disputed", "Indeterminate"}


class NaturalEarthConnector:
    def describe(self) -> ConnectorSpec:
        return ConnectorSpec(
            name="naturalearth.admin0_110m",
            version="1.0.0",
            template="naturalearth_asset",
            source_key="naturalearth.admin0_110m",
            provider="Natural Earth",
            licence="Public domain",
            allowed_hosts=("naciscdn.org",),
            dataset_ids=("ne_110m_admin_0_countries",),
            field_mappings={"match_key": "ADM0_A3"},
            units={},
            geography=("world",),
            time_semantics="release-versioned display geometry; no statistical validity period",
            lineage_family="natural_earth",
            cadence_seconds=7 * 86400,
            revision_strategy="conditional GET; new artifact only on changed bytes",
            max_response_bytes=10_000_000,
            max_pages=1,
            parser_version="ne-parse-1",
            code_digest=module_digest(NaturalEarthConnector),
        )

    def plan(self, cursor: Cursor | None, budget: FetchBudget) -> FetchPlan:
        state = cursor.state if cursor else {}
        params = {k: state[k] for k in ("etag", "last_modified") if state.get(k)}
        return FetchPlan(
            connector="naturalearth.admin0_110m",
            items=(FetchItem(key="admin0_110m", url=ASSET_URL, max_bytes=10_000_000,
                             expected_media_types=("application/zip", "application/octet-stream"),
                             params=params),),
            pagination="single asset",
        )

    async def fetch(self, item: FetchItem, http: Any) -> RawAcquisition:
        headers = {}
        if item.params.get("etag"):
            headers["If-None-Match"] = item.params["etag"]
        if item.params.get("last_modified"):
            headers["If-Modified-Since"] = item.params["last_modified"]
        result = await http.get(item.url, max_bytes=item.max_bytes, headers=headers, accept_status=(200, 304))
        return to_raw(item, result, getattr(http, "origin", "live"))

    @staticmethod
    def to_geojson(body: bytes) -> dict[str, Any]:
        members = read_zip_members(body, (".shp", ".shx", ".dbf", ".prj", ".cpg", ".txt"))
        shp = next((v for k, v in members.items() if k.endswith(".shp")), None)
        shx = next((v for k, v in members.items() if k.endswith(".shx")), None)
        dbf = next((v for k, v in members.items() if k.endswith(".dbf")), None)
        if not (shp and shx and dbf):
            raise SourceResponseError("Natural Earth archive lacks shp/shx/dbf", retryable=False, kind="schema_drift")
        version = next((v.decode("utf-8", "replace").strip() for k, v in members.items()
                        if k.upper().endswith("VERSION.TXT")), None)
        reader = shapefile.Reader(shp=io.BytesIO(shp), shx=io.BytesIO(shx), dbf=io.BytesIO(dbf), encoding="utf-8")
        fields = [f[0] for f in reader.fields[1:]]
        features = []
        for sr in reader.iterShapeRecords():
            rec = dict(zip(fields, sr.record, strict=True))
            props = {k: rec.get(k) for k in KEEP_PROPS if k in rec}
            features.append({"type": "Feature", "properties": props, "geometry": sr.shape.__geo_interface__})
        return {"type": "FeatureCollection", "features": features, "ne_version": version,
                "crs": "EPSG:4326 (lon,lat)"}

    def parse(self, raw: RawAcquisition) -> Iterator[ObservationBatch]:
        if raw.response_metadata.get("status") == 304:
            yield ObservationBatch(connector="naturalearth.admin0_110m", connector_version="1.0.0",
                                   item_key=raw.item.key, observations=(), metadata={"not_modified": True})
            return
        gj = self.to_geojson(raw.body)
        fields = sorted({k for f in gj["features"] for k in f["properties"]})
        yield ObservationBatch(
            connector="naturalearth.admin0_110m",
            connector_version="1.0.0",
            item_key=raw.item.key,
            observations=(),
            metadata={"feature_count": len(gj["features"]), "ne_version": gj["ne_version"], "fields": fields,
                      "types": sorted({str(f["properties"].get("TYPE")) for f in gj["features"]})},
            schema_fingerprint=schema_fingerprint(fields),
        )

    def validate(self, batch: ObservationBatch) -> QualityReport:
        if batch.metadata.get("not_modified"):
            return QualityReport(outcome="pass")
        findings = []
        if batch.metadata.get("feature_count", 0) < 150:
            findings.append(Finding(code="incomplete", severity="blocker", message="too few features"))
        if "ADM0_A3" not in batch.metadata.get("fields", []):
            findings.append(Finding(code="schema_drift", severity="blocker", message="ADM0_A3 missing"))
        return QualityReport(outcome="blocked" if findings else "pass", findings=tuple(findings),
                             received_count=batch.metadata.get("feature_count", 0))

    def next_cursor(self, plan: FetchPlan, outcomes: Sequence[FetchOutcome]) -> Cursor:
        return Cursor(state={"complete": all(o.status in ("published", "unchanged") for o in outcomes)})


def classify_feature(props: dict[str, Any]) -> str:
    # Only Natural Earth TYPE Disputed/Indeterminate marks a dispute; NOTE_BRK is a display note.
    if str(props.get("TYPE")) in DISPUTED_TYPES:
        return "disputed"
    return "unmatched"
