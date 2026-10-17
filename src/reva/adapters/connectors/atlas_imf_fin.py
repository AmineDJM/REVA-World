"""IMF finance datasets -> atlas tables (V6, agent V6FINDATA): COFER (currency composition of official foreign
exchange reserves), Global Debt Database, Historical Public Debt.

Source: the IMF data portal SDMX 2.1 REST API (``https://api.imf.org/external/sdmx/2.1``, open, no key), same
source key ``imf.sdmx``, budget and SDMX-ML parser (:func:`atlas_imf.parse_sdmx`) as A-IMF / V4ECON, geography
scheme ``imf`` (ISO3 + IMF aliases; aggregates such as ``G001`` World stay unmapped).

=============  ==================  ========================================================================  ======
table          IMF dataflow        content                                                                   rows
=============  ==================  ========================================================================  ======
``imf.cofer``  IMF.STA:COFER       world (G001), advanced (G110) and emerging & developing (G200) economies: ~2.3 k
                                   total / allocated / unallocated FX reserves and claims by currency (USD,
                                   EUR, CNY, JPY, GBP, AUD, CAD, CHF, other), US dollars and shares, quarterly
``imf.gdd``    IMF.FAD:GDD         Global Debt Database: public and private debt in % of GDP by sector       ~38 k
                                   (general / central government, public sector, NFCs, households), annual
``imf.hpd``    IMF.FAD:HPD         Historical Public Debt: general government gross debt, % of fiscal-year   ~9.6 k
                                   GDP, annual (from the 1800s for some economies)
=============  ==================  ========================================================================  ======

Rules: ``OBS_VALUE`` verbatim (strict decimal); absent -> no row (counted). Values are in full units (US dollars
for COFER amounts); the series ``SCALE`` is the IMF display scale, kept in ``flags.display_scale`` and never
applied. ``DERIVATION_TYPE`` kept with its quoted IMF label (``SC`` = IMF staff calculations, e.g. the COFER
imputed-reserves share from 2025-Q3); COFER notes that individual reporters are confidential (aggregates only).
Codes of folded dimensions must be in the quoted codelists below (fail closed). GDD series carry their
``DATA_SOURCE`` text (verbatim, ``flags.data_source``); the observation-level ``SOURCE`` attribute is **not** copied:
in the 2025-09-17 release every GDD observation carries ``SOURCE="NSATH"`` (CL_ORGANIZATION "National Statistical
Office (Thailand)") whatever the country, which reads as a placeholder, not a source.

As-of: ``flags.as_of`` = fetch date; the dataset ``UPDATE_DATE`` / ``PUBLICATION_DATE`` of the IMF message are in
``flags.provider_update_date``; a new release registers a new table version (vintages = table versions). The IMF
also publishes dated vintage dataflows (e.g. ``COFER_2025_OCT_VINTAGE``): not read here.
Licence: IMF Copyright and Usage terms with the provider LICENSE attribute "© International Monetary Fund
Copyright. All Rights Reserved."; datasets openly published (no key, citation requested) — same pending
maintainer review as ``registry/sources/imf.yaml`` (``flags.licence_review`` on every row).
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from reva.adapters.connectors import atlas_imf as imf
from reva.adapters.connectors import atlas_imf_hf as imf_hf
from reva.adapters.connectors import findata_common as fd
from reva.adapters.connectors import hf_daily_common as hc
from reva.atlas.sources import AtlasSourceSpec

NAME = "atlas.imf_fin"
VERSION = "1.0.1"  # 1.0.0 was registered in the dev DB from an earlier working-tree state
PARSER_VERSION = "atlas_imf_fin/1"
ATLAS_SOURCE = "imf"
SOURCE_KEY = imf.SOURCE_KEY
GEO_SCHEME = imf.GEO_SCHEME
BASE = imf.BASE
DATASETS = ("cofer", "gdd", "hpd")
MEDIA = ("application/vnd.sdmx.structurespecificdata+xml", "application/xml")
LICENCE = imf.LICENCE

_BASE: dict[str, Any] = {
    "licence": LICENCE,
    "licence_url": "https://www.imf.org/external/terms.htm",
    "attribution": "International Monetary Fund, <dataset>, https://data.imf.org/en/datasets/<agency>:<flow>",
    "provider_license_attribute": "© International Monetary Fund Copyright. All Rights Reserved.",
    "licence_review": "pending maintainer review (IMF terms page not retrievable from the build container, HTTP 403;"
    " datasets PUBLIC_OPEN, citation requested)",
    "licence_checked": "2026-10-17",
    "provider": "International Monetary Fund",
    "pub_lag": "not_stated",
    "revision_policy": "not stated per observation; the IMF replaces the dataflow at each release (dataset "
    "UPDATE_DATE in flags); value as fetched (as_of), earlier vintages kept as table versions",
}
RIGHTS: dict[str, dict[str, Any]] = {
    "cofer": {**_BASE, "publication": "quarterly COFER release (IMF Statistics Department survey; reported in US "
              "dollars at end-of-period market exchange rates)"},
    "gdd": {**_BASE, "publication": "annual Global Debt Database release (IMF Fiscal Affairs Department)"},
    "hpd": {**_BASE, "publication": "Historical Public Debt database release (IMF Fiscal Affairs Department)"},
}  # fmt: skip

DERIVATION = imf_hf.DERIVATION_LABELS


@dataclass(frozen=True)
class ImfFlow:
    dataset: str
    agency: str
    flow: str
    title: str
    dims: tuple[str, ...]  # SDMX key order, COUNTRY first, FREQUENCY last
    key: str  # full SDMX key ('' parts = all)
    fold: tuple[str, ...]  # first = indicator prefix
    labels: Mapping[str, Mapping[str, str]]  # quoted IMF codelist labels (every code the table may hold)
    freq: str
    unit_of: Mapping[str, str]  # code of unit_dim -> unit text
    unit_dim: str
    series_texts: tuple[str, ...] = ()  # series attributes copied verbatim into flags
    notes: str = ""

    @property
    def url(self) -> str:
        return f"{BASE}/data/{self.agency},{self.flow}/{self.key}"


_CI = {
    "CI_AUD": "Claims in Australian dollar", "CI_CAD": "Claims in Canadian dollar",
    "CI_OTHC": "Claims in Other currencies", "CI_DEM": "Claims in Deutsche mark", "CI_JPY": "Claims in Japanese yen",
    "CI_CHF": "Claims in Swiss franc", "CI_CNY": "Claims in Chinese yuan renminbi", "CI_FRF": "Claims in French franc",
    "CI_GBP": "Claims in Pound sterling", "CI_USD": "Claims in US dollar", "CI_ECU": "Claims in ECU",
    "CI_EUR": "Claims in Euro", "CI_NLG": "Claims in Dutch guilder", "CI_T": "Claims in All currencies",
}  # fmt: skip
_GDD = {
    "FL_S1311MIXED_POGDP_PT": "Debt instruments, Central government, Percent of GDP",
    "FL_S13_POGDP_PT": "Debt instruments, General government, Percent of GDP",
    "FL_S14_POGDP_PT": "Debt instruments, Households, Percent of GDP",
    "F3T4_S14_POGDP_PT": "Debt securities and loans, Households, Percent of GDP",
    "FL_S11_POGDP_PT": "Debt instruments, Non-financial corporations, Percent of GDP",
    "F3T4_S11_POGDP_PT": "Debt securities and loans, Non-financial corporations, Percent of GDP",
    "FL_S11001_POGDP_PT": "Debt instruments, Public non-financial Sector, Percent of GDP",
    "FL_S1ZS_POGDP_PT": "Debt instruments, Public sector, Percent of GDP",
    "FL_PS_POGDP_PT": "Debt instruments, Private sector, Percent of GDP",
    "F3T4_PS_POGDP_PT": "Debt securities and loans, Private sector, Percent of GDP",
}

FLOWS: tuple[ImfFlow, ...] = (
    ImfFlow(
        dataset="cofer", agency="IMF.STA", flow="COFER",
        title="Currency Composition of Official Foreign Exchange Reserves (COFER)",
        dims=("COUNTRY", "INDICATOR", "FXR_CURRENCY", "TYPE_OF_TRANSFORMATION", "FREQUENCY"),
        key="....Q",
        fold=("INDICATOR", "FXR_CURRENCY", "TYPE_OF_TRANSFORMATION"),
        labels={
            "INDICATOR": {"AFXRA": "Allocated foreign exchange reserves", "TFXRA": "Total foreign exchange reserves",
                          "UFXRA": "Unallocated foreign exchange reserves",
                          "TFXRA_IMP": "Foreign exchange reserves that have been imputed"},
            "FXR_CURRENCY": _CI,
            "TYPE_OF_TRANSFORMATION": {"SHRO_PT": "Shares", "NV_USD": "Nominal value"},
        },
        freq="Q",
        unit_dim="TYPE_OF_TRANSFORMATION",
        unit_of={"NV_USD": "US dollars", "SHRO_PT": "percent (share)"},
        notes="Aggregates only (G001 world, G110 advanced economies, G200 emerging market and developing economies);"
        " individual reporters are confidential (IMF). Shares of allocated reserves by currency; TFXRA_IMP = share of"
        " reserves imputed by IMF staff (derivation SC) from 2025-Q3.",
    ),
    ImfFlow(
        dataset="gdd", agency="IMF.FAD", flow="GDD", title="Global Debt Database (GDD)",
        dims=("COUNTRY", "INDICATOR", "FREQUENCY"),
        key="." + "+".join(_GDD) + ".A",
        fold=("INDICATOR",),
        labels={"INDICATOR": _GDD},
        freq="A",
        unit_dim="INDICATOR",
        unit_of={k: "percent of GDP" for k in _GDD},
        series_texts=("DATA_SOURCE",),
        notes="Debt-to-GDP ratios only (the GDD GDP series B1GQ_* are not taken: WEO/PWT copies). Sector coverage"
        " differs by country (central vs general government; FL = all debt instruments, F3T4 = securities and loans).",
    ),
    ImfFlow(
        dataset="hpd", agency="IMF.FAD", flow="HPD", title="Historical Public Debt (HPD)",
        dims=("COUNTRY", "INDICATOR", "FREQUENCY"),
        key=".G63G_S13_POFYGDP.A",
        fold=("INDICATOR",),
        labels={"INDICATOR": {"G63G_S13_POFYGDP": "Gross debt, General government, Percent of fiscal year GDP"}},
        freq="A",
        unit_dim="INDICATOR",
        unit_of={"G63G_S13_POFYGDP": "percent of fiscal-year GDP"},
        notes="General government gross debt as % of fiscal-year GDP; the period is the IMF's annual label (fiscal-"
        "year economies are not re-windowed: flags.period_basis = provider_annual_label).",
    ),
)  # fmt: skip
FLOWS_BY_DATASET = {f.dataset: f for f in FLOWS}
FILES: tuple[hc.FileSpec, ...] = tuple(
    hc.FileSpec(f"data:{f.flow}", f.url, (f.dataset,), MEDIA, 40_000_000) for f in FLOWS
)


def code_digest() -> str:
    return hc.code_digest(fd, imf, imf_hf, sys.modules[__name__])


def atlas_source() -> AtlasSourceSpec:
    return AtlasSourceSpec(
        name=NAME,
        version=VERSION,
        source_key=SOURCE_KEY,
        budget_key=SOURCE_KEY,
        atlas_source=ATLAS_SOURCE,
        provider="International Monetary Fund (data portal SDMX 2.1 API): COFER, Global Debt Database, Historical "
        "Public Debt",
        licence=LICENCE,
        allowed_hosts=(imf.HOST,),
        datasets=DATASETS,
        dataset_ids=tuple(f"{f.agency}:{f.flow}" for f in FLOWS),
        cadence_seconds=30 * hc.DAY,
        planned_requests=len(FILES),
        max_response_bytes=40_000_000,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=code_digest(),
        field_mappings={"geo_scheme": GEO_SCHEME, "tables": [f"imf.{d}" for d in DATASETS]},
        notes="3 SDMX-ML requests per full run (~10 MB measured 2026-10-17)",
    )


def parse(fl: ImfFlow, f: hc.Fetched, rows: fd.PeriodRows) -> dict[str, Any]:
    try:
        msg = imf.parse_sdmx(f.body)
    except imf.SdmxError as e:
        raise hc.err(f"IMF {fl.flow}: {e}") from e
    if msg.structure.get("id") != fl.flow:
        raise hc.err(f"IMF {fl.flow}: response is for another dataflow ({msg.flow_ref})")
    upd = msg.dataset_attrs.get("UPDATE_DATE", "")
    rep: Counter[str] = Counter()
    geos: set[str] = set()
    for sattrs, obs in msg.series:
        rep["series"] += 1
        missing = [d for d in fl.dims if d not in sattrs]
        if missing:
            raise hc.err(f"IMF {fl.flow}: series without dimension(s) {missing}")
        if sattrs["FREQUENCY"] != fl.freq:
            raise hc.err(f"IMF {fl.flow}: unrequested frequency {sattrs['FREQUENCY']!r}")
        for d, lab in fl.labels.items():
            if sattrs[d] not in lab:
                raise hc.err(f"IMF {fl.flow}: {d} code {sattrs[d]!r} not in the quoted codelist")
        geo = sattrs["COUNTRY"]
        indicator = "|".join([sattrs[fl.fold[0]], *(f"{d.lower()}={sattrs[d]}" for d in fl.fold[1:])])
        unit = fl.unit_of[sattrs[fl.unit_dim]]
        base: dict[str, Any] = {
            "frequency": fl.freq,
            "precision": fd.PRECISION[fl.freq],
            "flow": msg.flow_ref,
            "provider_update_date": upd,
            "seasonal_adjustment": "not_stated_by_provider",
        }
        for d in fl.fold:
            base[f"{d.lower()}_label"] = fl.labels[d][sattrs[d]]
        if sattrs.get("SCALE") not in (None, "", "0"):
            base["display_scale"] = sattrs["SCALE"]
        for a in (*fl.series_texts, "METHODOLOGY", "OVERLAP"):
            if sattrs.get(a):
                base[a.lower()] = sattrs[a][:1000]
        if fl.dataset == "hpd":
            base["period_basis"] = "provider_annual_label"
        skey = ".".join(sattrs[d] for d in fl.dims)
        for o in obs:
            tp = o.get("TIME_PERIOD", "")
            val = imf.decimal_text(o.get("OBS_VALUE"))
            if val is None:
                rep["no_value"] += 1
                continue
            start, end = imf_hf.imf_period(fl.freq, tp)
            extra = dict(base)
            dt = o.get("DERIVATION_TYPE")
            if dt:
                extra["derivation_type"] = f"{dt}: {DERIVATION.get(dt, '(not in the quoted IMF codelist)')}"
                if dt in ("SE", "SEME", "SEHI"):
                    extra["estimate_kind"] = "estimate"
                elif dt == "SP":
                    raise hc.err(f"IMF {fl.flow}: staff projection in an observation table ({skey} {tp})")
            if o.get("STATUS"):
                extra["provider_status"] = o["STATUS"]
            rows.add_period(indicator=indicator, provider_geo=geo, start=start, end=end, value=val, unit=unit,
                            flags=rows.flags(**hc.base_flags(RIGHTS[fl.dataset], f, **extra)), raw_sha256=f.sha256,
                            selector={"file": f.key, "flow": msg.flow_ref, "series": skey, "time_period": tp})  # fmt: skip
            rep["rows"] += 1
            geos.add(geo)
    return {"flow": msg.flow_ref, "update_date": upd, "geos": len(geos), **fd.counter_report(rep)}


def build(files: Mapping[str, hc.Fetched], wanted: tuple[str, ...]) -> tuple[dict[str, hc.TableBuild], dict[str, Any]]:
    out: dict[str, hc.TableBuild] = {}
    report: dict[str, Any] = {}
    for ds in wanted:
        fl = FLOWS_BY_DATASET[ds]
        rows = fd.PeriodRows(ATLAS_SOURCE, ds)
        report[ds] = parse(fl, files[f"data:{fl.flow}"], rows)
        out[ds] = hc.TableBuild(rows)
    return out, report


NOTES = {f.dataset: f"IMF {f.agency}:{f.flow} '{f.title}' ({f.url}). {f.notes} Values verbatim in full units "
         f"(display SCALE never applied); observations without OBS_VALUE are not rows. Licence: {LICENCE} (review "
         "pending, flags.licence_review)." for f in FLOWS}  # fmt: skip


async def atlas_ingest(run: Any) -> dict[str, Any]:
    return await fd.ingest_files(run, FILES, DATASETS, build, rights=RIGHTS, notes=NOTES, source_key=SOURCE_KEY,
                                 geo_scheme=GEO_SCHEME)  # fmt: skip


__all__ = ["DATASETS", "FILES", "FLOWS", "NAME", "RIGHTS", "atlas_ingest", "atlas_source", "build"]
