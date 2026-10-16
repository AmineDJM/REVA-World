"""Atlas gap matrix (agent GAPHUNT): which of the 251 crosswalk entities have NO value, from ANY ingested source,
for the owner's headline concepts — and which sources were searched for each gap.

Coverage of a concept for an entity = at least one non-projection cell (numeric value or text) of one of the
concept's series. Two levels are reported per concept:

* ``headline``: the series of the REVA headline concept(s) (``registry/atlas/source_order``, ATLASOPS) — what the
  best-available view can answer (snapshot mode also counts ATLASOPS's same-source derived values);
* ``any``: the headline series plus every other ingested series that reports the same kind of figure under
  another definition (CIA Factbook, UNSD AMA, GeoNames, WEO population, GAPHUNT gap-fill offices …). A gap at
  this level means *no source REVA holds* reports anything for the entity.

Coverage inputs (each records where it came from; several are merged by union):

``snapshot``  an atlas snapshot of a database (DuckDB query file of :mod:`reva.atlas.query`) — the reference
              once the deployment has a snapshot: ``--snapshot [--db NAME] [--origin live]``.
``tables``    the latest atlas tables of a database without a snapshot, read from its artifact store:
              ``--tables --db NAME --objects DIR`` (read-only SELECTs; nothing is written to the database).
``static``    report-derived coverage of sources not loaded anywhere (World Bank lists of docs/dev/v3_a_wb.md,
              SSPTW retirement ages of docs/dev/v3_a_law.md): ``--static``.
``coverage``  coverage JSON written earlier by this script (``--save-coverage``): ``--coverage FILE``.

Output: ``--out-md`` (markdown matrix + searched sources) and ``--out-json`` (machine-readable: per concept and
per entity, the series consulted and the sources searched, for the UI's "searched sources" panel).

    uv run python scripts/atlas_gap_matrix.py --static --tables --db reva --objects var/objects \
        --out-md /tmp/gaps.md --out-json /tmp/gaps.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CROSSWALK = REPO / "registry" / "entities" / "country_crosswalk.json"

# ------------------------------------------------------------------------------------------- concepts
#: key -> (labels, headline concept ids, other series patterns). A pattern is "source.dataset.indicator"; a
#: trailing "*" matches any indicator with that prefix.
CONCEPTS: dict[str, dict[str, Any]] = {
    "population": {
        "en": "Population",
        "fr": "Population",
        "headline": ["reva.population.total"],
        "other": [
            "un_wpp.wpp2024_demo.TPopulation1Jan",
            "imf.weo.LP",
            "factbook.figures_2026_01.population.total",
            "unsd.ama.xr_pop|Population",
            "unsd.syb68_t02.Population*",
            "geonames.countryinfo.population",
            "spc.pocket.MIDYEARPOPEST",
            "eurostat.nuts2_gapfill.demo_r_d2jan|unit=NR|sex=T|age=TOTAL",
            "cbs.bes_population.population_1_january",
        ],
    },
    "area": {
        "en": "Area",
        "fr": "Superficie",
        "headline": ["reva.geography.surface_area", "reva.geography.land_area"],
        "other": [
            "factbook.figures_2026_01.area.total",
            "geonames.countryinfo.area_km2",
            "naturalearth.admin0_10m.area_km2",
            "unsd.syb68_t02.Surface area*",
            "spc.pocket.LAR",
            "eurostat.nuts2_gapfill.reg_area3*",
        ],
    },
    "density": {
        "en": "Population density",
        "fr": "Densité",
        "headline": ["reva.population.density"],
        "other": [
            "un_wpp.wpp2024_demo.PopDensity",
            "unsd.syb68_t02.Population density*",
            "spc.pocket.POPDENS",
            "eurostat.nuts2_gapfill.demo_r_d3dens|unit=PER_KM2",
        ],
    },
    "gdp": {
        "en": "GDP",
        "fr": "PIB",
        "headline": ["reva.economy.gdp_usd"],
        "other": [
            "unsd.ama.gdp_current_usd|Gross Domestic Product (GDP)",
            "imf.weo.PPPGDP",
            "factbook.figures_2026_01.gdp.real_ppp",
            "spc.pocket.GDPCUSD",
            "spc.pocket.GDPCDOM",
            "cbs.bes_gdp.gdp_current_prices",
            "eurostat.nuts2_gapfill.nama_10r_2gdp|unit=MIO_EUR",
        ],
    },
    "gdp_per_capita": {
        "en": "GDP per capita",
        "fr": "PIB par habitant",
        "headline": ["reva.economy.gdp_per_capita_usd", "reva.economy.gdp_per_capita_ppp"],
        "other": [
            "unsd.ama.gdp_pc_current_usd",
            "factbook.figures_2026_01.gdp.real_per_capita_ppp",
            "spc.pocket.GDPCPCUSD",
            "cbs.bes_gdp.gdp_per_capita",
            "eurostat.nuts2_gapfill.nama_10r_2gdp|unit=EUR_HAB",
        ],
    },
    "unemployment": {
        "en": "Unemployment rate",
        "fr": "Taux de chômage",
        "headline": ["reva.labour.unemployment_rate", "reva.labour.unemployment_rate_national"],
        "other": [
            "imf.weo.LUR",
            "factbook.figures_2026_01.unemployment_rate",
            "un_sdg.sdg_headline.SL_TLF_UEM*",
            "spc.emprates.UNEMPRATE|sex=_T|age=_T|urbanization=_T|disability=_T",
            "eurostat.nuts2_gapfill.lfst_r_lfu3rt*",
            "cbs.bes_labour.unemployment_rate*",
        ],
    },
    "minimum_wage": {
        "en": "Minimum wage",
        "fr": "Salaire minimum",
        "headline": ["reva.labour.minimum_wage_monthly_lcu"],
        "other": ["oecd.earnings_mw_curp.*", "insee.smic_dom.*"],
    },
    "average_wage": {
        "en": "Average wage",
        "fr": "Salaire moyen",
        "headline": ["reva.labour.average_monthly_earnings_lcu"],
        "other": ["oecd.earnings_av_an_wage.*", "ilostat.ear_emtm_sex_cur_nb_a.*", "statsjersey.earnings.*"],
    },
    "life_expectancy": {
        "en": "Life expectancy",
        "fr": "Espérance de vie",
        "headline": ["reva.health.life_expectancy"],
        "other": [
            "who.gho.WHOSIS_000001*",
            "factbook.figures_2026_01.life_expectancy.total",
            "spc.pocket.LEBM",
            "spc.pocket.LEBF",
            "eurostat.nuts2_gapfill.demo_r_mlifexp*",
        ],
    },
    "literacy_schooling": {
        "en": "Literacy / schooling",
        "fr": "Alphabétisation / scolarité",
        "headline": ["reva.education.literacy_rate_adult"],
        "other": [
            "factbook.figures_2026_01.literacy.total",
            "factbook.figures_2026_01.school_life_expectancy.total",
            "uis.api.MYS.1T8.AG25T99",
            "undp.hdr25.mys",
        ],
    },
    "hdi": {
        "en": "HDI-like index",
        "fr": "Indice de type IDH",
        "headline": ["reva.society.hdi"],
        "other": ["undp.hdr25.phdi"],
    },
    "inflation": {
        "en": "Inflation",
        "fr": "Inflation",
        "headline": ["reva.economy.inflation_cpi"],
        "other": [
            "imf.weo.PCPIEPCH",
            "factbook.figures_2026_01.inflation",
            "spc.pocket.INFRATE",
            "insee.ipc_dom.CPI_ANNUAL_CHANGE*",
        ],
    },
    "government_balance": {
        "en": "Government balance",
        "fr": "Solde public",
        "headline": ["reva.finance.government_net_lending_gdp"],
        "other": ["imf.weo.GGXONLB_NGDP", "imf.gfs_soo.GNLB_T*", "factbook.figures_2026_01.budget.revenues"],
    },
    "trade_balance": {
        "en": "Trade balance",
        "fr": "Balance commerciale",
        "headline": [
            "reva.trade.goods_balance_usd",
            "reva.trade.goods_services_balance_usd",
            "reva.trade.current_account_gdp",
            "reva.trade.merchandise_balance_imts",
        ],
        "other": [
            "imf.imts.XG_FOB_USD|partner=G001",
            "wb.wdi_econ.NE.RSB.GNFS.CD",
            "wb.wdi_econ.BN.CAB.XOKA.CD",
            "factbook.figures_2026_01.exports",
            "spc.pocket.TRADEBALUSD",
        ],
    },
    "co2": {
        "en": "CO2 emissions",
        "fr": "Émissions de CO2",
        "headline": ["reva.environment.co2_per_capita"],
        "other": ["owid.co2.co2", "factbook.figures_2026_01.co2_emissions", "un_sdg.sdg_headline.EN_ATM_CO2*"],
    },
    "internet": {
        "en": "Internet use",
        "fr": "Usage d'Internet",
        "headline": ["reva.science.internet_users"],
        "other": [
            "factbook.figures_2026_01.internet_users",
            "spc.digital.DD024",
            "eurostat.nuts2_gapfill.isoc_r_iuse_i*",
        ],
    },
    "languages": {
        "en": "Languages",
        "fr": "Langues",
        "headline": [],
        "other": [
            "factbook.texts_2026_01.people_and_society.languages*",
            "geonames.countryinfo.languages",
            "spc.keyfacts.LAN",
        ],
    },
    "capital": {
        "en": "Capital",
        "fr": "Capitale",
        "headline": [],
        "other": [
            "factbook.texts_2026_01.government.capital.name",
            "geonames.countryinfo.capital",
            "un_wup.wup2025_capitals.*",
            "spc.keyfacts.CAP",
        ],
    },
    "currency": {
        "en": "Currency",
        "fr": "Monnaie",
        "headline": [],
        "other": [
            "factbook.texts_2026_01.economy.exchange_rates.currency",
            "geonames.countryinfo.currency_code",
            "spc.keyfacts.CUR",
        ],
    },
    "legal_system": {
        "en": "Legal system",
        "fr": "Système juridique",
        "headline": [],
        "other": ["factbook.legal_2026_01.legal_system"],
    },
    "retirement_age": {
        "en": "Retirement age",
        "fr": "Âge de la retraite",
        "headline": [],
        "other": [
            "wbl.wbl2026.Pen_Law_PensAgeEq_FullM",
            "wbl.wbl2026.Pen_Law_PensAgeEq_PartM",
            "ssa.ssptw_2018_2019.old_age.pension_age.men",
        ],
    },
    "voting_age": {
        "en": "Voting age",
        "fr": "Âge du droit de vote",
        "headline": [],
        "other": [
            "factbook.legal_2026_01.suffrage.minimum_age",
            "factbook.legal_2026_01.suffrage",
            "ccp.characteristics_v5.votemin",
        ],
    },
}

# ------------------------------------------------------------------------------------- static inputs
#: World Bank series coverage from docs/dev/v3_a_wb.md (live ingest 2026-10-15): the 217 WB economies minus the
#: economies the report lists without any value. Only series whose missing list the report gives are included.
WB_MINUS: dict[str, list[str]] = {
    "wb.wdi_social.SP.POP.TOTL": [],
    "wb.wdi_social.EN.POP.DNST": ["XKX"],
    "wb.wdi_social.AG.LND.TOTL.K2": ["XKX"],
    "wb.wdi_social.AG.SRF.TOTL.K2": ["XKX"],
    "wb.wdi_social.SL.UEM.TOTL.ZS": "ABW AND ASM ATG BMU CUW CYM DMA FRO FSM GIB GRD GRL IMN KIR KNA LIE MAF MCO MHL MNP NRU PLW SMR SXM SYC TCA TUV VGB XKX".split(),
    "wb.wdi_social.SL.UEM.TOTL.NE.ZS": "AND CAF CHI ERI MAF PRK SXM VIR".split(),
    "wb.wdi_econ.NY.GDP.MKTP.CD": ["GIB", "PRK", "VGB"],
    "wb.wdi_econ.NY.GDP.PCAP.CD": ["GIB", "PRK", "VGB"],
    "wb.wdi_econ.NE.RSB.GNFS.CD": "AND BDI BRB CHI DMA GIB GRD IMN JAM KNA LBR LCA LIE MAF MCO MMR PRK SXM TCA TTO TUV VCT VGB".split(),
    "wb.wdi_econ.BN.GSR.GNFS.CD": "ASM CHI CUB GIB GRL GUM IMN LIE MAF MCO MNP PRI PRK SOM TKM VGB VIR".split(),
    "wb.wdi_econ.BN.CAB.XOKA.CD": "ASM CHI CUB GIB GRL GUM IMN LIE MAF MCO MNP PRI PRK SOM TKM VGB VIR".split(),
}
#: SSPTW 2018-19 pension ages for economies WBL lacks (docs/dev/v3_a_law.md, "Gaps" section).
SSPTW_FILLS = "AND BGD BTN CUB LBN LIE MCO MMR TKM ZAF".split()


def entities() -> dict[str, dict[str, Any]]:
    doc = json.loads(CROSSWALK.read_text())
    return {e["key"]: e for e in doc["entities"]}


def static_coverage() -> dict[str, dict[str, Any]]:
    ents = entities()
    wb = sorted(k for k, e in ents.items() if e.get("worldbank"))
    out: dict[str, dict[str, Any]] = {}
    for key, minus in WB_MINUS.items():
        out[key] = {
            "iso3": sorted(set(wb) - set(minus)),
            "origin": "static: docs/dev/v3_a_wb.md (live ingest 2026-10-15)",
        }
    out["ssa.ssptw_2018_2019.old_age.pension_age.men"] = {
        "iso3": SSPTW_FILLS,
        "origin": "static: docs/dev/v3_a_law.md (SSPTW ages for economies WBL lacks; other SSPTW economies not listed)",
    }
    return out


# ------------------------------------------------------------------------------------- live inputs
_PROJ = ('"projection":true', '"estimate_kind":"projection"', '"period_after_vintage":true')


def _patterns() -> list[str]:
    from reva.atlas.source_order import load_headlines

    reg = load_headlines()
    pats: set[str] = set()
    for spec in CONCEPTS.values():
        pats.update(spec["other"])
        for cid in spec["headline"]:
            c = reg.concepts[cid]
            pats.update(s.series.key for s in c.series)
            if c.derived:
                for pair in c.derived.alternatives:
                    for o in pair:
                        if o.series is not None:
                            pats.add(o.series.key)
    return sorted(pats)


def _match(key: str, pat: str) -> bool:
    return key.startswith(pat[:-1]) if pat.endswith("*") else key == pat


def tables_coverage(db: str, objects: Path) -> dict[str, dict[str, Any]]:
    """Latest atlas table per dataset key of a database (read-only), read from its artifact store."""
    import polars as pl
    from sqlalchemy import create_engine, text

    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.atlas.tables import read_atlas_table
    from reva.settings import Settings

    base = Settings(_env_file=REPO / ".env")
    eng = create_engine(base.migration_url().set(database=db))
    store = FilesystemArtifactStore(objects, base.reva_s3_prefix)
    pats = _patterns()
    prefixes = {".".join(p.split(".")[:2]) for p in pats}
    with eng.connect() as c:
        rows = c.execute(
            text(
                """SELECT DISTINCT ON (dataset_key) dataset_key, artifact_sha256, recorded_at FROM evidence.dataset
                   WHERE concept_id = 'atlas.table' ORDER BY dataset_key, recorded_at DESC"""
            )
        ).all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        sd = r.dataset_key.removeprefix("atlas.")
        if sd not in prefixes:
            continue
        f = read_atlas_table(store, r.artifact_sha256)
        f = f.filter(
            pl.col("iso3").is_not_null()
            & (pl.col("value").is_not_null() | pl.col("value_text").is_not_null())
            & ~pl.any_horizontal([pl.col("flags").str.contains(p, literal=True) for p in _PROJ])
        )
        ind = f.select("indicator").unique()["indicator"].to_list()
        want = [i for i in ind if any(_match(f"{sd}.{i}", p) for p in pats)]
        if not want:
            continue
        g = f.filter(pl.col("indicator").is_in(want)).group_by("indicator").agg(pl.col("iso3").unique())
        for i, isos in g.iter_rows():
            out[f"{sd}.{i}"] = {
                "iso3": sorted(isos),
                "origin": f"tables: db {db}, {r.dataset_key} recorded {r.recorded_at:%Y-%m-%d}",
            }
    return out


def snapshot_coverage(
    db: str, objects: Path, origin: str | None
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Series coverage + headline coverage (incl. derived cells) of the latest recorded atlas snapshot."""
    from sqlalchemy import create_engine

    from reva.adapters.objects.filesystem import FilesystemArtifactStore
    from reva.atlas.query import AtlasQuery
    from reva.atlas.snapshot import resolve_snapshot
    from reva.atlas.source_order import load_headlines
    from reva.settings import Settings

    base = Settings(_env_file=REPO / ".env")
    eng = create_engine(base.migration_url().set(database=db))
    store = FilesystemArtifactStore(objects, base.reva_s3_prefix)
    with eng.connect() as c:
        snap = resolve_snapshot(c, store, origin=origin)
    q = AtlasQuery(snap, store)
    pats = _patterns()
    sids = [sid for sid, (s, d, i) in q.series_of.items() if any(_match(f"{s}.{d}.{i}", p) for p in pats)]
    out: dict[str, dict[str, Any]] = {}
    if sids:
        proj = " OR ".join(f"contains(flags, '{p}')" for p in _PROJ)
        rows = q._rows(
            f"SELECT sid, list(DISTINCT iso3) AS isos FROM cells WHERE iso3 IS NOT NULL AND has_value "
            f"AND NOT ({proj}) AND sid IN ({','.join(str(s) for s in sids)}) GROUP BY sid"
        )
        for r in rows:
            s, d, i = q.series_of[r["sid"]]
            out[f"{s}.{d}.{i}"] = {"iso3": sorted(r["isos"]), "origin": f"snapshot {snap.sha256[:12]} ({db})"}
    reg = load_headlines()
    head = {
        c["concept_id"]: sorted(set(_all_keys()) - set(c["countries_without_value"])) for c in q.headline_coverage(reg)
    }
    q.close()
    return out, head


def _all_keys() -> list[str]:
    return sorted(entities())


# ---------------------------------------------------------------------------------------- matrix
def matrix(cov: dict[str, dict[str, Any]], head_snapshot: dict[str, list[str]] | None = None) -> dict[str, Any]:
    from reva.atlas.source_order import load_headlines

    reg = load_headlines()
    ents = entities()
    allk = set(ents)
    res: dict[str, Any] = {"concepts": {}, "entities": {}}
    for key, spec in CONCEPTS.items():
        head_series: list[str] = []
        for cid in spec["headline"]:
            head_series += [s.series.key for s in reg.concepts[cid].series]

        def covered(pats: Iterable[str]) -> tuple[set[str], dict[str, int]]:
            got: set[str] = set()
            per: dict[str, int] = {}
            for sk, v in cov.items():
                if any(_match(sk, p) for p in pats):
                    got |= set(v["iso3"])
                    per[sk] = len(v["iso3"])
            return got & allk, per

        h, h_per = covered(head_series)
        if head_snapshot:
            for cid in spec["headline"]:
                h |= set(head_snapshot.get(cid, []))
        a, a_per = covered(head_series + spec["other"])
        a |= h
        res["concepts"][key] = {
            "labels": {"en": spec["en"], "fr": spec["fr"]},
            "headline_concepts": spec["headline"],
            "series_consulted": head_series + spec["other"],
            "series_with_data": dict(sorted({**h_per, **a_per}.items())),
            "headline_covered": len(h),
            "any_covered": len(a),
            "headline_missing": sorted(allk - h),
            "any_missing": sorted(allk - a),
        }
    for k in sorted(allk):
        res["entities"][k] = {
            "label": ents[k].get("label"),
            "missing_any": [c for c, v in res["concepts"].items() if k in v["any_missing"]],
            "missing_headline": [c for c, v in res["concepts"].items() if k in v["headline_missing"]],
        }
    return res


def to_markdown(res: dict[str, Any]) -> str:
    lines = [
        "| concept | headline: entities with a value | any source: with a value | no value from any source (ISO3) |",
        "|---|---:|---:|---|",
    ]
    for v in res["concepts"].values():
        miss = " ".join(v["any_missing"]) or "—"
        lines.append(f"| {v['labels']['en']} | {v['headline_covered']} | {v['any_covered']} | {miss} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--static", action="store_true", help="include report-derived coverage")
    ap.add_argument("--tables", action="store_true", help="read the latest atlas tables of --db from --objects")
    ap.add_argument("--snapshot", action="store_true", help="use the latest atlas snapshot of --db")
    ap.add_argument("--origin", default=None)
    ap.add_argument("--db", default="reva")
    ap.add_argument("--objects", type=Path, default=REPO / "var" / "objects")
    ap.add_argument("--coverage", action="append", default=[], type=Path, help="coverage JSON from --save-coverage")
    ap.add_argument("--save-coverage", type=Path)
    ap.add_argument("--out-json", type=Path)
    ap.add_argument("--out-md", type=Path)
    a = ap.parse_args(argv)
    cov: dict[str, dict[str, Any]] = {}
    head = None

    def merge(more: dict[str, dict[str, Any]]) -> None:
        for k, v in more.items():
            if k in cov:
                cov[k] = {
                    "iso3": sorted(set(cov[k]["iso3"]) | set(v["iso3"])),
                    "origin": f"{cov[k]['origin']}; {v['origin']}",
                }
            else:
                cov[k] = v

    if a.static:
        merge(static_coverage())
    for p in a.coverage:
        merge(json.loads(p.read_text()))
    if a.tables:
        merge(tables_coverage(a.db, a.objects))
    if a.snapshot:
        more, head = snapshot_coverage(a.db, a.objects, a.origin)
        merge(more)
    if not cov:
        ap.error("no coverage input: give --static, --tables, --snapshot or --coverage")
    if a.save_coverage:
        a.save_coverage.write_text(json.dumps(cov, indent=0, sort_keys=True))
    res = matrix(cov, head)
    res["inputs"] = sorted({o.strip() for v in cov.values() for o in v["origin"].split(";")})
    if a.out_json:
        a.out_json.write_text(json.dumps(res, indent=1, ensure_ascii=False))
    md = to_markdown(res)
    if a.out_md:
        a.out_md.write_text(md)
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
