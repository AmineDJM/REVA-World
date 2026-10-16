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
        "en": "Government balance / budget",
        "fr": "Solde public / budget",
        "headline": ["reva.finance.government_net_lending_gdp"],
        "other": [
            "imf.weo.GGXONLB_NGDP",
            "imf.gfs_soo.GNLB_T*",
            "factbook.figures_2026_01.budget.revenues",
            "spc.pocket.GOVREVGDPC",
        ],
    },
    "trade_balance": {
        "en": "Trade balance / exports-imports",
        "fr": "Balance commerciale / exportations-importations",
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

# ---------------------------------------------------------------------------------- searched sources
#: Every source GAPHUNT searched for the gaps (2026-10-16), with the result: ``used`` (atlas connector),
#: ``skipped`` (reason) or ``no_data``. ``targets`` = entities it was searched for; shown per gap by the UI.
SEARCHED: list[dict[str, Any]] = [
    {
        "id": "spc.pdh_stat",
        "name": "Pacific Community (SPC) Pacific Data Hub .Stat",
        "url": "https://stats-sdmx-disseminate.pacificdata.org/rest/",
        "licence": "PDH catalogue 'Other (Open)', per-dataset licence (pending review)",
        "targets": "ASM COK FJI PYF GUM KIR MHL FSM NRU NCL NIU MNP PLW PNG PCN WSM SLB TKL TON TUV VUT WLF".split(),
        "result": "used",
        "connector": "atlas.gap_spc",
        "note": "DF_POCKET, DF_KEYFACTS, DF_EMPRATES, DF_DIGITAL_DEVELOPMENT; live run pending budget key spc.pdh_stat",
    },
    {
        "id": "eurostat.nuts2",
        "name": "Eurostat regional statistics (NUTS-2)",
        "url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/",
        "licence": "Eurostat reuse policy (CC BY 4.0)",
        "targets": "GLP MTQ GUF REU MYT ALA SJM".split(),
        "result": "used",
        "connector": "atlas.gap_eurostat_nuts",
        "note": "ingested live 2026-10-16 (budget eurostat.dissemination)",
    },
    {
        "id": "insee.bdm",
        "name": "Insee BDM (SDMX)",
        "url": "https://bdm.insee.fr/series/sdmx/",
        "licence": "Licence Ouverte 2.0 (Etalab)",
        "targets": "GLP MTQ GUF REU MYT".split(),
        "result": "used",
        "connector": "atlas.gap_insee",
        "note": "CPI (annual average change) and Smic of the overseas departments; live run pending budget key insee.bdm",
    },
    {
        "id": "cbs.opendata",
        "name": "CBS Statistics Netherlands StatLine (OData)",
        "url": "https://opendata.cbs.nl/ODataApi/odata/",
        "licence": "CC BY 4.0",
        "targets": ["BES"],
        "result": "used",
        "connector": "atlas.gap_cbs_bes",
        "note": "population, GDP (USD), labour survey; CPI only quarterly per island (not taken); live run pending budget key cbs.opendata",
    },
    {
        "id": "statistics_jersey.opendata",
        "name": "Statistics Jersey (opendata.gov.je, CKAN)",
        "url": "https://opendata.gov.je/",
        "licence": "Open Government Licence - Jersey v1.0",
        "targets": ["JEY"],
        "result": "used",
        "connector": "atlas.gap_jersey",
        "note": "monthly FTE earnings (second source after ILOSTAT); RPI is not a CPI (not taken); registered unemployment is not ILO (not taken)",
    },
    {
        "id": "insee.local",
        "name": "Insee populations légales / COM (Saint-Martin, Saint-Barthélemy, Saint-Pierre-et-Miquelon)",
        "url": "https://www.insee.fr/fr/statistiques/2011101?geo=COM-97701",
        "licence": "Licence Ouverte 2.0",
        "targets": "MAF BLM SPM".split(),
        "result": "skipped",
        "note": "census figures are HTML pages per collectivity, no SDMX series in the BDM; population already in WPP/Factbook",
    },
    {
        "id": "ieom",
        "name": "IEOM / IEDOM (overseas issuing institutes)",
        "url": "https://www.ieom.fr/",
        "licence": "not stated for data reuse",
        "targets": "NCL PYF WLF SPM BLM MAF".split(),
        "result": "skipped",
        "note": "annual reports in PDF; no machine-readable open series found",
    },
    {
        "id": "isee.nc",
        "name": "ISEE Nouvelle-Calédonie",
        "url": "https://www.isee.nc/",
        "licence": "not verified",
        "targets": ["NCL"],
        "result": "skipped",
        "note": "data.isee.nc unreachable from the container (connection failed); NCL already covered by WB/AMA/SPC",
    },
    {
        "id": "ispf",
        "name": "ISPF Polynésie française",
        "url": "https://www.ispf.pf/",
        "licence": "not verified",
        "targets": ["PYF"],
        "result": "skipped",
        "note": "Excel/PDF publications; PYF covered by WB, AMA, SPC",
    },
    {
        "id": "census.idb",
        "name": "US Census Bureau International Database (IDB) API",
        "url": "https://api.census.gov/data/timeseries/idb/5year",
        "licence": "US public domain",
        "targets": "GGY JEY IMN AIA MSR SPM WLF SHN FLK MAF BLM PRI GUM VIR ASM MNP".split(),
        "result": "skipped",
        "note": "API now answers 302 to missing_key.html: a key is required (registration with an e-mail address; not done, no personal data in requests); no bulk file found",
    },
    {
        "id": "census.decennial",
        "name": "US Census Bureau (Puerto Rico, Guam, USVI, CNMI, American Samoa)",
        "url": "https://data.census.gov/",
        "licence": "US public domain",
        "targets": "PRI GUM VIR MNP ASM".split(),
        "result": "skipped",
        "note": "same API key requirement; island areas already in WB/WPP; SPC covers GUM/MNP/ASM",
    },
    {
        "id": "stat.gl",
        "name": "Statistics Greenland (PxWeb)",
        "url": "https://bank.stat.gl/api/v1/en/Greenland/",
        "licence": "not verified",
        "targets": ["GRL"],
        "result": "skipped",
        "note": "PxWeb v1 data queries need POST (the atlas platform is GET-only); remaining GRL gaps (minimum/average wage, literacy, HDI) are not published as such",
    },
    {
        "id": "hagstova.fo",
        "name": "Statistics Faroe Islands (PxWeb)",
        "url": "https://statbank.hagstova.fo/api/v1/en/H2/",
        "licence": "not verified",
        "targets": ["FRO"],
        "result": "skipped",
        "note": "PxWeb v1 data queries need POST (GET-only platform)",
    },
    {
        "id": "ask.rks",
        "name": "Kosovo Agency of Statistics (ASKdata PxWeb)",
        "url": "https://askdata.rks-gov.net/api/v1/en/ASKdata/",
        "licence": "not verified",
        "targets": ["XKX"],
        "result": "skipped",
        "note": "PxWeb v1 data queries need POST; XKX gaps are minimum wage, literacy and HDI",
    },
    {
        "id": "ssb.no",
        "name": "Statistics Norway (Svalbard)",
        "url": "https://data.ssb.no/api/pxwebapi/v2/",
        "licence": "CC BY 4.0 (SSB)",
        "targets": ["SJM"],
        "result": "skipped",
        "note": "Eurostat NO0B already gives SJM population, area, GDP; PxWeb v2 GET exists (candidate for Svalbard settlements detail)",
    },
    {
        "id": "asub.ax",
        "name": "Statistics and Research Åland (ÅSUB, PxWeb)",
        "url": "https://pxweb.asub.ax/PXWeb/api/v1/en/",
        "licence": "not verified",
        "targets": ["ALA"],
        "result": "skipped",
        "note": "PxWeb v1 POST; Eurostat FI20 covers ALA population, area, GDP, life expectancy",
    },
    {
        "id": "gov.gg",
        "name": "Guernsey data and analysis",
        "url": "https://www.gov.gg/data",
        "licence": "not verified",
        "targets": ["GGY"],
        "result": "skipped",
        "note": "bulletins in PDF/Excel only",
    },
    {
        "id": "gov.im",
        "name": "Isle of Man Economic Affairs",
        "url": "https://www.gov.im/",
        "licence": "not verified",
        "targets": ["IMN"],
        "result": "skipped",
        "note": "HTTP 403 to automated requests",
    },
    {
        "id": "gibraltar.gov.gi",
        "name": "HM Government of Gibraltar statistics",
        "url": "https://www.gibraltar.gov.gi/statistics",
        "licence": "not verified",
        "targets": ["GIB"],
        "result": "skipped",
        "note": "PDF abstracts only",
    },
    {
        "id": "gov.bm",
        "name": "Bermuda Department of Statistics",
        "url": "https://www.gov.bm/department/statistics",
        "licence": "not verified",
        "targets": ["BMU"],
        "result": "skipped",
        "note": "PDF reports; BMU covered by WB/AMA/IMF BOP",
    },
    {
        "id": "eso.ky",
        "name": "Cayman Islands Economics and Statistics Office",
        "url": "https://www.eso.ky/",
        "licence": "not verified",
        "targets": ["CYM"],
        "result": "skipped",
        "note": "PDF/Excel; CYM covered by WB/AMA",
    },
    {
        "id": "falklands.gov.fk",
        "name": "Falkland Islands Government statistics",
        "url": "https://www.falklands.gov.fk/policy/statistics",
        "licence": "not verified",
        "targets": ["FLK"],
        "result": "skipped",
        "note": "unreachable from the container (connection failed)",
    },
    {
        "id": "cbs.aw",
        "name": "CBS Aruba",
        "url": "https://cbs.aw/wp/",
        "licence": "not verified",
        "targets": ["ABW"],
        "result": "skipped",
        "note": "Excel/PDF downloads, no API; ABW covered by WB",
    },
    {
        "id": "cbs.cw",
        "name": "CBS Curaçao",
        "url": "https://www.cbs.cw/",
        "licence": "not verified",
        "targets": ["CUW"],
        "result": "skipped",
        "note": "Excel/PDF downloads, no API; CUW covered by WB/AMA",
    },
    {
        "id": "stat.gov.sx",
        "name": "STAT Sint Maarten",
        "url": "https://stat.gov.sx/",
        "licence": "not verified",
        "targets": ["SXM"],
        "result": "skipped",
        "note": "unreachable from the container",
    },
    {
        "id": "dgbas.tw",
        "name": "Taiwan DGBAS national statistics",
        "url": "https://nstatdb.dgbas.gov.tw/dgbasall/webMain.aspx?k=engmain",
        "licence": "Open Government Data License v1.0 (data.gov.tw) for data.gov.tw datasets",
        "targets": ["TWN"],
        "result": "skipped",
        "note": "TWN gaps are literacy, HDI and average wage in ILOSTAT form: DGBAS computes a 'Taiwan HDI' with UNDP's method (a national computation, not UNDP's HDI) and publishes it in PDF/HTML; eng.stat.gov.tw unreachable; data.gov.tw reachable but no machine-readable HDI/literacy series found",
    },
    {
        "id": "censtatd.hk",
        "name": "Hong Kong Census and Statistics Department (API)",
        "url": "https://www.censtatd.gov.hk/api/",
        "licence": "HK Government data terms",
        "targets": ["HKG"],
        "result": "no_data",
        "note": "API works; HKG has no remaining gap in the 22 concepts (every concept has at least one source)",
    },
    {
        "id": "dsec.mo",
        "name": "Macao DSEC",
        "url": "https://www.dsec.gov.mo/",
        "licence": "not verified",
        "targets": ["MAC"],
        "result": "skipped",
        "note": "MAC gaps: minimum wage (statutory since 2020) and retirement age; DSEC publishes neither as a series (legal texts)",
    },
    {
        "id": "imsee.mc",
        "name": "Monaco IMSEE",
        "url": "https://www.imsee.mc/",
        "licence": "not verified",
        "targets": ["MCO"],
        "result": "skipped",
        "note": "PDF publications (Monaco en chiffres)",
    },
    {
        "id": "statistica.sm",
        "name": "San Marino statistics office",
        "url": "https://www.statistica.sm/",
        "licence": "not verified",
        "targets": ["SMR"],
        "result": "skipped",
        "note": "HTML/PDF only; SMR gap is CO2",
    },
    {
        "id": "llv.li",
        "name": "Liechtenstein Amt für Statistik",
        "url": "https://www.llv.li/",
        "licence": "not verified",
        "targets": ["LIE"],
        "result": "skipped",
        "note": "HTTP 403 to automated requests",
    },
    {
        "id": "estadistica.ad",
        "name": "Andorra Departament d'Estadística",
        "url": "https://www.estadistica.ad/",
        "licence": "not verified",
        "targets": ["AND"],
        "result": "skipped",
        "note": "HTML/PxWeb (POST); AND has no remaining any-source gap (WDI/ILO modelled unemployment missing, Factbook has it)",
    },
    {
        "id": "vatican",
        "name": "Holy See / Vatican City State",
        "url": "https://www.vatican.va/",
        "licence": "-",
        "targets": ["VAT"],
        "result": "no_data",
        "note": "no statistical office and no national accounts, labour or price statistics exist; Factbook population and area only",
    },
    {
        "id": "bok.ecos",
        "name": "Bank of Korea (North Korea GDP estimates)",
        "url": "https://ecos.bok.or.kr/api/",
        "licence": "API key required",
        "targets": ["PRK"],
        "result": "skipped",
        "note": "an external estimate (not an official DPRK statistic); API needs a key and was unreachable; PRK GDP already in UNSD AMA and Factbook",
    },
    {
        "id": "esh",
        "name": "Western Sahara",
        "url": "-",
        "licence": "-",
        "targets": ["ESH"],
        "result": "no_data",
        "note": "no official statistics office for the territory; only the (stale, 2021) Factbook profile and UN WPP population",
    },
    {
        "id": "pcbs.ps",
        "name": "Palestinian Central Bureau of Statistics",
        "url": "https://www.pcbs.gov.ps/",
        "licence": "not verified",
        "targets": ["PSE"],
        "result": "skipped",
        "note": "unreachable from the container; PSE remaining gaps are legal system and voting age (Factbook has only West Bank/Gaza partial profiles)",
    },
    {
        "id": "stat.gov.ct.tr",
        "name": "Northern Cyprus statistics (unrecognised; not a crosswalk entity)",
        "url": "https://www.stat.gov.ct.tr/",
        "licence": "not verified",
        "targets": [],
        "result": "skipped",
        "note": "unreachable; would stay a separate unmapped provider geography, never merged into CYP",
    },
    {
        "id": "somaliland",
        "name": "Somaliland (unrecognised; not a crosswalk entity)",
        "url": "-",
        "licence": "-",
        "targets": [],
        "result": "skipped",
        "note": "no open machine-readable statistics found; WHR/NE keep 'Somaliland' unmapped, never merged into SOM",
    },
    {
        "id": "uninhabited",
        "name": "Uninhabited / no permanent population",
        "url": "-",
        "licence": "-",
        "targets": "ATA BVT HMD SGS IOT UMI ATF".split(),
        "result": "no_data",
        "note": "no resident population: no economy, labour, price, health or legal statistics exist; area (and GeoNames/Factbook texts) only",
    },
]

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
PROPOSALS = REPO / "registry" / "atlas" / "source_order" / "_proposals_gaphunt.yaml"
#: GAPHUNT's proposed new headline concepts -> the matrix concept they would answer
PROPOSED_NEW = {
    "reva.population.total_1_january": "population",
    "reva.health.life_expectancy_national_tables": "life_expectancy",
}


def proposal_series() -> dict[str, list[str]]:
    """Matrix concept -> series GAPHUNT proposes to ATLASOPS (``_proposals_gaphunt.yaml``), for ``--with-proposals``."""
    import yaml

    doc = yaml.safe_load(PROPOSALS.read_text())
    by_head: dict[str, list[str]] = {}
    for p in doc.get("add_series", []):
        by_head.setdefault(p["concept"], []).append(p["entry"]["series"])
    out: dict[str, list[str]] = {}
    for key, spec in CONCEPTS.items():
        for cid in spec["headline"]:
            out.setdefault(key, []).extend(by_head.get(cid, []))
    for c in doc.get("new_concepts", []):
        key = PROPOSED_NEW.get(c["concept"]["id"])
        if key:
            out.setdefault(key, []).extend(s["series"] for s in c["concept"]["series"])
    return out


def matrix(
    cov: dict[str, dict[str, Any]],
    head_snapshot: dict[str, list[str]] | None = None,
    proposals: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    from reva.atlas.source_order import load_headlines

    reg = load_headlines()
    ents = entities()
    allk = set(ents)
    res: dict[str, Any] = {"concepts": {}, "entities": {}}
    for key, spec in CONCEPTS.items():
        head_series: list[str] = []
        for cid in spec["headline"]:
            head_series += [s.series.key for s in reg.concepts[cid].series]
        head_series += (proposals or {}).get(key, [])

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
        head = v["headline_covered"] if v["headline_concepts"] else "–"
        lines.append(f"| {v['labels']['en']} | {head} | {v['any_covered']} | {miss} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--static", action="store_true", help="include report-derived coverage")
    ap.add_argument(
        "--with-proposals", action="store_true", help="count GAPHUNT's proposed headline series as headline"
    )
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
    res = matrix(cov, head, proposal_series() if a.with_proposals else None)
    res["with_proposals"] = bool(a.with_proposals)
    res["searched_sources"] = SEARCHED
    for k, e in res["entities"].items():
        e["searched"] = [x["id"] for x in SEARCHED if k in x["targets"]]
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
