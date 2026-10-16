"""V-Dem (Varieties of Democracy) -> atlas tables (V3, agent A-POL).

Source
------
V-Dem Country-Year **Full+Others** dataset v16 (1789-2025, 202 country units incl. 26 historical
states), licence **CC BY-SA 4.0** (V-Dem FAQ, read 2026-10-15). The v-dem.net download form asks
for an e-mail address, gender and consent to personal-data processing; an autonomous agent does not
submit the owner's personal data, so this connector uses the V-Dem Institute's *own* distribution of
the same dataset in its R package ``vdemdata`` on GitHub (``data/vdem.RData``, ``data/codebook.RData``
and ``DESCRIPTION``, which states the package/data version). The RData files are read by
:mod:`reva.adapters.connectors.atlas_rdata` (no R needed).

What is loaded (and what is not)
--------------------------------
* ``vdem.indices`` -- V-Dem's own indices (``v2x*``: the five high-level democracy indices,
  mid-level components, civil liberties, corruption, rule of law, accountability, women's political
  empowerment, neopatrimonialism, ...), each with its posterior-interval bounds ``*_codelow`` /
  ``*_codehigh`` when V-Dem publishes them, and the Regimes of the World classification
  (``v2x_regime``, ``v2x_regime_amb``) with the category label in ``value_text``.
* ``vdem.institutions`` / ``vdem.liberties`` -- a curated set of V-Dem expert-coded (C) and factual (A) indicators on the
  political system: elections, executive, legislature, judiciary, parties, civil liberties, media,
  civil society, corruption, state. For C variables: the measurement-model point estimate (latent
  scale), its interval bounds and the original-scale posterior (``*_osp``). Factual variables as
  coded; nominal/ordinal ones also carry the category label in ``value_text``; names and titles of
  heads of state/government and of chambers are ``value_text``.
* **Not loaded**: the third-party "Others" variables ``e_*`` (Polity5 -- redistribution prohibited
  by the Center for Systemic Peace; Freedom House; WGI -- agent A-WB; UDS; TI CPI; BMR and LIED --
  loaded from their own CC0 releases by :mod:`atlas_regimes`; economic/demographic series -- other
  agents). V-Dem's licence does not cover them.

Semantics
---------
* One cell = V-Dem country unit x calendar year (``period = [Y-01-01, Y+1-01-01)``). V-Dem's own
  country-year aggregation of the variable (day-weighted mean, last, maximum, ...; codebook
  ``cy_aggregation``) applies; election-specific variables are reported in election years only and
  are **not** filled forward (V-Dem's ``fill_vars`` is not applied).
* Country units follow V-Dem's continuity rules: ``DEU`` 1789-1870 is the Kingdom of Prussia, ``RUS``
  1922-1991 the USSR, ``MEX`` 1789-1820 the Viceroyalty of New Spain, etc. Every row carries
  ``flags.vdem_histname`` (V-Dem's name of the polity in that year) so this is never hidden.
* ``iso3`` is filled through the ``vdem`` geo scheme (``registry/atlas/geo_aliases/a_pol.yaml``):
  V-Dem's ``country_text_id`` when it is a crosswalk key, except ``PSE`` (V-Dem codes the West Bank
  only; Gaza is ``PSG``), which is kept unmapped. Historical units (Baden, Bavaria, Papal States, GDR,
  South Yemen, Republic of Vietnam, ...) are kept with ``iso3 = null``; ``entity_qid`` comes from
  ``registry/entities/vdem_qids.json`` (agent-mapped). ``TWN`` maps to the crosswalk entity TWN since
  crosswalk 1.1.0 for all its years (V-Dem's unit is the territory of Taiwan); its 1945-1948 rows are
  V-Dem's "Republic of China [headquartered in the mainland]" (``flags.vdem_histname``, entity_qid
  Q13426199), never silently the post-1949 state.
* Values are the exact shortest decimal text of V-Dem's stored doubles (``repr``), never rounded;
  NA cells are omitted (the provider reports nothing).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

from reva.adapters.connectors import atlas_rdata
from reva.adapters.connectors.atlas_rdata import RFrame, StrColumn, column_text, read_rdata_frame
from reva.adapters.connectors.base import to_raw
from reva.kernel.errors import SourceResponseError
from reva.ports.connectors import FetchItem, RawAcquisition

SOURCE = "vdem"
SOURCE_KEY = "vdem.vdemdata"
CONNECTOR_NAME = "atlas.vdem"
CONNECTOR_VERSION = "1.0.1"  # ATLASOPS: patch bump, code changed after 1.0.0 was registered in the dev DB (TWNMAP/agent edits)
PARSER_VERSION = "atlas_vdem/1"
HOST = "raw.githubusercontent.com"
BASE_URL = f"https://{HOST}/vdeminstitute/vdemdata/master"
EXPECTED_MAJOR_VERSION = 16  # vdemdata DESCRIPTION "Version: 16.0" (dataset v16, 2026-03)
MAX_RDATA_BYTES = 52_000_000  # vdem.RData v16 = 33.9 MB
GEO_SCHEME = "vdem"

REPO_ROOT = Path(__file__).resolve().parents[4]
QIDS_PATH = REPO_ROOT / "registry" / "entities" / "vdem_qids.json"

RIGHTS: dict[str, Any] = {
    "licence": "CC-BY-SA-4.0",
    "licence_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "attribution": (
        "Coppedge et al. 2026. V-Dem Country-Year Dataset v16. Varieties of Democracy (V-Dem) Project. "
        "https://doi.org/10.23696/vdemds26 ; Pemstein et al. 2026, The V-Dem Measurement Model."
    ),
    "share_alike": "derived tables must be shared under CC BY-SA 4.0",
    "distribution": "V-Dem Institute R package vdemdata (GitHub), data/vdem.RData + data/codebook.RData",
}


class VdemParseError(SourceResponseError):
    pass


def _err(msg: str, kind: str = "schema_drift") -> VdemParseError:
    return VdemParseError(msg, retryable=False, kind=kind)


# ----------------------------------------------------------------------------- fetch plan
def fetch_items() -> tuple[FetchItem, ...]:
    rdata = ("application/octet-stream",)
    return (
        FetchItem(
            key="vdemdata.description",
            url=f"{BASE_URL}/DESCRIPTION",
            max_bytes=100_000,
            expected_media_types=("text/plain",),
        ),
        FetchItem(
            key="vdemdata.codebook",
            url=f"{BASE_URL}/data/codebook.RData",
            max_bytes=5_000_000,
            expected_media_types=rdata,
        ),
        FetchItem(
            key="vdemdata.vdem",
            url=f"{BASE_URL}/data/vdem.RData",
            max_bytes=MAX_RDATA_BYTES,
            expected_media_types=rdata,
        ),
    )


async def fetch(item: FetchItem, http: Any) -> RawAcquisition:
    result = await http.get(item.url, max_bytes=item.max_bytes)
    return to_raw(item, result, getattr(http, "origin", "live"))


async def fetch_all(http: Any) -> dict[str, RawAcquisition]:
    """Every item of :func:`fetch_items`, sequentially (one host at a time; nothing on failure)."""
    return {it.key: await fetch(it, http) for it in fetch_items()}


def atlas_source() -> Any:
    """The atlas source spec (discovered by :mod:`reva.atlas.sources`; budget key = source key)."""
    from reva.atlas.sources import AtlasSourceSpec

    return AtlasSourceSpec(
        name=CONNECTOR_NAME,
        version=CONNECTOR_VERSION,
        source_key=SOURCE_KEY,
        atlas_source=SOURCE,
        provider="V-Dem Institute (University of Gothenburg)",
        licence="CC-BY-SA-4.0",
        allowed_hosts=(HOST,),
        datasets=DATASETS,
        dataset_ids=("vdemdata:data/vdem.RData", "vdemdata:data/codebook.RData"),
        cadence_seconds=30 * 86400,
        planned_requests=len(fetch_items()),
        max_response_bytes=MAX_RDATA_BYTES,
        min_interval_seconds=1.0,
        parser_version=PARSER_VERSION,
        code_digest=_code_digest(),
        field_mappings={"rdata_object": "vdem", "unit": "country_text_id", "period": "year", "geo_scheme": GEO_SCHEME},
        notes="CC BY-SA 4.0 (share alike). Third-party e_* variables are not loaded.",
    )


def _code_digest() -> str:
    h = hashlib.sha256()
    for mod in (atlas_rdata, __import__(__name__, fromlist=["_"])):
        h.update(inspect.getsource(mod).encode())
    return "sha256:" + h.hexdigest()


# ----------------------------------------------------------------------------- variable selection
#: V-Dem indices loaded into ``vdem.indices`` (point estimate; bounds added when published).
INDEX_VARS: tuple[str, ...] = (
    # high-level democracy indices
    "v2x_polyarchy",
    "v2x_libdem",
    "v2x_partipdem",
    "v2x_delibdem",
    "v2x_egaldem",
    "v2x_api",
    "v2x_mpi",
    # mid-level components / sub-indices
    "v2x_freexp_altinf",
    "v2x_frassoc_thick",
    "v2x_suffr",
    "v2xel_frefair",
    "v2x_elecoff",
    "v2x_liberal",
    "v2xcl_rol",
    "v2x_jucon",
    "v2xlg_legcon",
    "v2x_partip",
    "v2x_cspart",
    "v2xdd_dd",
    "v2xel_locelec",
    "v2xel_regelec",
    "v2xdl_delib",
    "v2x_egal",
    "v2xeg_eqprotec",
    "v2xeg_eqaccess",
    "v2xeg_eqdr",
    "v2x_EDcomp_thick",
    "v2x_freexp",
    "v2xme_altinf",
    # accountability
    "v2x_accountability",
    "v2x_veracc",
    "v2x_diagacc",
    "v2x_horacc",
    # executive selection (dimension indices of the electoral regime)
    "v2x_ex_hereditary",
    "v2x_ex_military",
    "v2x_ex_party",
    "v2x_ex_direlect",
    "v2x_ex_confidence",
    # neopatrimonialism
    "v2x_neopat",
    "v2xnp_regcorr",
    "v2xnp_pres",
    "v2xnp_client",
    # civil liberties
    "v2x_civlib",
    "v2x_clpriv",
    "v2x_clpol",
    "v2x_clphy",
    "v2xcl_acjst",
    "v2xcl_prpty",
    "v2xcl_dmove",
    "v2xcl_slave",
    "v2xcl_disc",
    # exclusion
    "v2xpe_exlsocgr",
    "v2xpe_exlgender",
    "v2xpe_exlecon",
    "v2xpe_exlpol",
    "v2xpe_exlgeo",
    # corruption
    "v2x_corr",
    "v2x_pubcorr",
    "v2x_execorr",
    # women's political empowerment
    "v2x_gender",
    "v2x_gencl",
    "v2x_gencs",
    "v2x_genpp",
    # rule of law, civil society, parties, federalism, academic freedom, electoral integrity
    "v2x_rule",
    "v2xcs_ccsi",
    "v2xps_party",
    "v2x_divparctrl",
    "v2x_feduni",
    "v2xca_academ",
    "v2x_electoral_integrity",
    # direct democracy components
    "v2xdd_i_ci",
    "v2xdd_i_rf",
    "v2xdd_i_or",
    "v2xdd_i_pl",
    "v2xdd_cic",
    "v2xdd_toc",
    # regime classification and electoral-regime dummies
    "v2x_regime",
    "v2x_regime_amb",
    "v2x_elecreg",
    "v2xex_elecreg",
    "v2xlg_elecreg",
    "v2xel_elecparl",
    "v2xel_elecpres",
    "v2xlg_leginter",
    "v2x_hosinter",
    "v2x_hosabort",
    "v2x_legabort",
    "v2xex_elecleg",
)

#: Curated V-Dem indicators: (variable, kind). Kinds: "C" expert-coded (point + bounds + _osp), "A"
#: factual numeric, "cat" factual category (value + label), "text" factual text (value_text only).
#: ``vdem.institutions``: elections, executive, legislature, judiciary, parties.
INSTITUTIONS: tuple[tuple[str, str], ...] = (
    # elections
    ("v2elfrfair", "C"),
    ("v2elmulpar", "C"),
    ("v2elembaut", "C"),
    ("v2elembcap", "C"),
    ("v2elrgstry", "C"),
    ("v2elvotbuy", "C"),
    ("v2elirreg", "C"),
    ("v2elintim", "C"),
    ("v2elpeace", "C"),
    ("v2elboycot", "C"),
    ("v2elaccept", "C"),
    ("v2elasmoff", "C"),
    ("v2elpubfin", "C"),
    ("v2eltrnout", "A"),
    ("v2elvaptrn", "A"),
    ("v2elsuffrage", "A"),
    ("v2elage", "A"),
    ("v2elfemrst", "cat"),
    ("v2elcomvot", "cat"),
    ("v2elparlel", "cat"),
    ("v2elloelsy", "cat"),
    ("v2elloseat", "A"),
    ("v2ellostsl", "A"),
    ("v2ellovtlg", "A"),
    ("v2elthresh", "A"),
    ("v2eltype_0", "cat"),
    ("v2eltype_1", "cat"),
    ("v2eltype_4", "cat"),
    ("v2eltype_5", "cat"),
    ("v2eltype_6", "cat"),
    ("v2eltype_7", "cat"),
    # executive
    ("v2exnamhos", "text"),
    ("v2extithos", "text"),
    ("v2exnamhog", "text"),
    ("v2extithog", "text"),
    ("v2exhoshog", "cat"),
    ("v2expathhs", "cat"),
    ("v2expathhg", "cat"),
    ("v2ex_hosw", "A"),
    ("v2ex_elechos", "cat"),
    ("v2ex_elechog", "cat"),
    ("v2ex_legconhos", "cat"),
    ("v2ex_legconhog", "cat"),
    ("v2exfemhos", "cat"),
    ("v2exfemhog", "cat"),
    ("v2exfxtmhs", "A"),
    ("v2exrescon", "C"),
    ("v2exbribe", "C"),
    ("v2exembez", "C"),
    ("v2excrptps", "C"),
    ("v2exthftps", "C"),
    # legislature
    ("v2lgbicam", "cat"),
    ("v2lgnamelo", "text"),
    ("v2lgnameup", "text"),
    ("v2lgfemleg", "A"),
    ("v2lgqugen", "cat"),
    ("v2lgello", "A"),
    ("v2lgelecup", "A"),
    ("v2lgotovst", "C"),
    ("v2lginvstp", "C"),
    ("v2lgqstexp", "C"),
    ("v2lgfunds", "C"),
    ("v2lgcrrpt", "C"),
    ("v2lgoppart", "C"),
    ("v2lgdomchm", "C"),
    # judiciary
    ("v2juhcind", "C"),
    ("v2juncind", "C"),
    ("v2jucomp", "C"),
    ("v2juhccomp", "C"),
    ("v2jureview", "C"),
    ("v2jucorrdc", "C"),
    ("v2jupurge", "C"),
    ("v2jupoatck", "C"),
    # parties
    ("v2psparban", "C"),
    ("v2psbars", "C"),
    ("v2psoppaut", "C"),
)
#: ``vdem.liberties``: civil liberties, media, civil society, state (+ regime duration).
LIBERTIES: tuple[tuple[str, str], ...] = (
    # civil liberties
    ("v2cltort", "C"),
    ("v2clkill", "C"),
    ("v2clrelig", "C"),
    ("v2cldiscm", "C"),
    ("v2cldiscw", "C"),
    ("v2clacfree", "C"),
    ("v2cldmovem", "C"),
    ("v2cldmovew", "C"),
    ("v2clslavem", "C"),
    ("v2clslavef", "C"),
    ("v2cltrnslw", "C"),
    ("v2clrspct", "C"),
    ("v2clacjstm", "C"),
    ("v2clacjstw", "C"),
    # media
    ("v2mecenefm", "C"),
    ("v2meharjrn", "C"),
    ("v2meslfcen", "C"),
    ("v2mecrit", "C"),
    ("v2merange", "C"),
    ("v2mecenefi", "C"),
    # civil society
    ("v2cseeorgs", "C"),
    ("v2csreprss", "C"),
    ("v2cscnsult", "C"),
    ("v2csprtcpt", "C"),
    # state
    ("v2svindep", "cat"),
    ("v2svstterr", "C"),
    ("v2svdomaut", "C"),
    ("v2svinlaut", "C"),
    ("v2regdur", "A"),
)
INDICATORS: tuple[tuple[str, str], ...] = INSTITUTIONS + LIBERTIES
DATASETS: tuple[str, ...] = ("indices", "institutions", "liberties")

#: Category labels (short forms written by A-POL from the V-Dem v16 codebook ``responses``; the full
#: response text is in the catalog). Keys are the reported codes as decimal text.
_YES_NO = {"0": "No", "1": "Yes"}
CATEGORY_LABELS: dict[str, dict[str, str]] = {
    "v2x_regime": {
        "0": "Closed autocracy",
        "1": "Electoral autocracy",
        "2": "Electoral democracy",
        "3": "Liberal democracy",
    },
    "v2x_regime_amb": {
        "0": "Closed autocracy",
        "1": "Closed autocracy upper bound",
        "2": "Electoral autocracy lower bound",
        "3": "Electoral autocracy",
        "4": "Electoral autocracy upper bound",
        "5": "Electoral democracy lower bound",
        "6": "Electoral democracy",
        "7": "Electoral democracy upper bound",
        "8": "Liberal democracy lower bound",
        "9": "Liberal democracy",
    },
    "v2x_elecreg": _YES_NO,
    "v2xex_elecreg": _YES_NO,
    "v2xlg_elecreg": _YES_NO,
    "v2elfemrst": {"0": "No female suffrage", "1": "Restricted female suffrage", "2": "Universal female suffrage"},
    "v2elcomvot": {
        "0": "No",
        "1": "Yes, without (enforced) sanctions",
        "2": "Yes, enforced minimal sanctions",
        "3": "Yes, enforced considerable sanctions",
    },
    "v2elparlel": {"0": "Majoritarian", "1": "Proportional", "2": "Mixed", "3": "Other"},
    "v2elloelsy": {
        "0": "First-past-the-post (single-member)",
        "1": "Two-round system (single-member)",
        "2": "Alternative vote (single-member)",
        "3": "Block vote (multi-member)",
        "4": "Party block vote (multi-member)",
        "5": "Parallel (SMD/PR)",
        "6": "Mixed-member proportional",
        "7": "List PR, small districts (mean < 7)",
        "8": "List PR, large districts (mean > 7)",
        "9": "Single transferable vote",
        "10": "Single non-transferable vote",
        "11": "Limited vote",
        "12": "Borda count",
    },
    "v2eltype_0": _YES_NO,
    "v2eltype_1": _YES_NO,
    "v2eltype_4": _YES_NO,
    "v2eltype_5": _YES_NO,
    "v2eltype_6": _YES_NO,
    "v2eltype_7": _YES_NO,
    "v2exhoshog": _YES_NO,
    "v2expathhs": {
        "0": "Force (coup or rebellion)",
        "1": "Appointed by a foreign power",
        "2": "Appointed by the ruling party (one-party system)",
        "3": "Appointed by a royal council",
        "4": "Hereditary succession",
        "5": "Appointed by the military",
        "6": "Appointed by the legislature",
        "7": "Directly through a popular election",
        "8": "Other",
    },
    "v2expathhg": {
        "0": "Force (coup or rebellion)",
        "1": "Appointed by a foreign power",
        "2": "Appointed by the ruling party (one-party system)",
        "3": "Appointed by a royal council",
        "4": "Hereditary succession",
        "5": "Appointed by the military",
        "6": "Appointed by the head of state",
        "7": "Appointed by the legislature",
        "8": "Directly through a popular election",
        "9": "Other",
    },
    "v2ex_elechos": _YES_NO,
    "v2ex_elechog": _YES_NO,
    "v2ex_legconhos": _YES_NO,
    "v2ex_legconhog": _YES_NO,
    "v2exfemhos": _YES_NO,
    "v2exfemhog": _YES_NO,
    "v2lgbicam": {"0": "0 chambers", "1": "1 chamber", "2": "2 or more chambers"},
    "v2lgqugen": {
        "0": "No national gender quota",
        "1": "Statutory quota, no sanctions",
        "2": "Statutory quota, weak sanctions",
        "3": "Statutory quota, strong sanctions",
        "4": "Reserved seats for women",
    },
    "v2svindep": _YES_NO,
}

#: Index variables that are categorical (value + value_text label).
CATEGORICAL_INDICES = frozenset({"v2x_regime", "v2x_regime_amb", "v2x_elecreg", "v2xex_elecreg", "v2xlg_elecreg"})

ID_COLUMNS = ("country_name", "country_text_id", "country_id", "year", "histname")


# ----------------------------------------------------------------------------- codebook / description
def parse_description(body: bytes) -> dict[str, str]:
    """``DESCRIPTION`` (Debian control format) -> fields; checks the package and major version."""
    fields: dict[str, str] = {}
    key = None
    for line in body.decode("utf-8").splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] += " " + line.strip()
        elif ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            fields[key] = val.strip()
    if fields.get("Package") != "vdemdata":
        raise _err(f"DESCRIPTION is not the vdemdata package ({fields.get('Package')!r})")
    m = re.match(r"^(\d+)(\.\d+)*$", fields.get("Version", ""))
    if not m:
        raise _err(f"vdemdata DESCRIPTION has no parsable Version ({fields.get('Version')!r})")
    return fields


def dataset_version(description: Mapping[str, str]) -> str:
    return description["Version"]


def parse_codebook(raw: bytes) -> dict[str, dict[str, str | None]]:
    """``codebook.RData`` -> {tag: {name, vartype, question, responses, scale, cy_aggregation, years, ...}}."""
    try:
        f = read_rdata_frame(raw, "codebook")
    except atlas_rdata.RDataError as e:
        raise _err(f"codebook.RData: {e}") from e
    for col in ("tag", "name", "question", "responses", "scale", "cy_aggregation", "years", "vartype"):
        if col not in f.columns:
            raise _err(f"codebook has no column {col!r}")
    out: dict[str, dict[str, str | None]] = {}
    for i in range(f.nrows):
        tag = column_text(f.columns["tag"], i)
        if tag:
            out[tag] = {c: column_text(f.columns[c], i) for c in f.all_columns}
    if len(out) < 300:
        raise _err(f"codebook has only {len(out)} variables; refusing a partial codebook")
    return out


# ----------------------------------------------------------------------------- columns
@dataclass(frozen=True)
class Series:
    """One V-Dem column loaded as an atlas indicator."""

    column: str  # V-Dem column (= atlas indicator code)
    base: str  # base variable (codebook tag)
    role: str  # point | codelow | codehigh | osp | cat | text | factual
    unit: str


#: Index scales that are not 0-1 (V-Dem v16 codebook ``scale``, checked against the observed range).
UNBOUNDED_INDICES = frozenset({"v2x_accountability", "v2x_veracc", "v2x_diagacc", "v2x_horacc", "v2x_divparctrl"})
INDEX_UNITS: dict[str, str] = {
    "v2xdd_i_rf": "index 0-2",
    "v2xdd_i_or": "index 0-2",
    "v2xdd_i_pl": "index 0-2",
    # codebook says "Interval, from low to high (0-1)" but v16 values reach 1.92: unit kept as the codebook
    # words, the discrepancy is reported (docs/dev/v3_a_pol.md), nothing is rescaled.
    "v2xdd_i_ci": "interval index (codebook: 0-1; v16 values up to 1.92)",
    "v2xex_elecleg": "interval index (codebook: 'Interval')",
}


def _index_unit(var: str) -> str:
    if var in CATEGORICAL_INDICES:
        return "category code"
    if var in UNBOUNDED_INDICES:
        return "V-Dem latent interval scale (unbounded)"
    return INDEX_UNITS.get(var, "index 0-1")


def plan_series(columns: Iterable[str], dataset: str) -> list[Series]:
    """The V-Dem columns of ``dataset`` present in this release (missing ones are reported, not guessed)."""
    cols = set(columns)
    out: list[Series] = []
    if dataset == "indices":
        for v in INDEX_VARS:
            if v not in cols:
                continue
            out.append(Series(v, v, "cat" if v in CATEGORICAL_INDICES else "point", _index_unit(v)))
            for b in ("codelow", "codehigh"):
                if f"{v}_{b}" in cols:
                    out.append(Series(f"{v}_{b}", v, b, _index_unit(v)))
            if v in UNBOUNDED_INDICES and f"{v}_osp" in cols:
                out.append(Series(f"{v}_osp", v, "osp", "index 0-1 (standard normal CDF of the latent scale)"))
    elif dataset in ("institutions", "liberties"):
        for v, kind in INSTITUTIONS if dataset == "institutions" else LIBERTIES:
            if v not in cols:
                continue
            if kind == "C":
                latent = v + "_osp" in cols
                unit = (
                    "V-Dem measurement-model latent scale"
                    if latent
                    else C_UNITS.get(v, "as coded (see codebook scale)")
                )
                out.append(Series(v, v, "point", unit))
                for b in ("codelow", "codehigh"):
                    if f"{v}_{b}" in cols:
                        out.append(Series(f"{v}_{b}", v, b, unit))
                if latent:
                    out.append(Series(v + "_osp", v, "osp", "original ordinal scale (posterior mean)"))
            elif kind == "cat":
                out.append(Series(v, v, "cat", "category code"))
            elif kind == "text":
                out.append(Series(v, v, "text", "text"))
            else:
                out.append(Series(v, v, "factual", _factual_unit(v)))
    else:
        raise ValueError(f"unknown V-Dem dataset {dataset!r}")
    return out


#: Units of C variables without an ordinal original scale (codebook "Percent").
C_UNITS: dict[str, str] = {"v2svstterr": "%"}

_PERCENT_VARS = {
    "v2eltrnout",
    "v2elvaptrn",
    "v2elsuffrage",
    "v2lgfemleg",
    "v2ellostsl",
    "v2ellovtlg",
    "v2elthresh",
    "v2lgello",
    "v2lgelecup",
}


def _factual_unit(v: str) -> str:
    if v in _PERCENT_VARS:
        return "%"
    if v in ("v2elage", "v2exfxtmhs"):
        return "years"
    if v == "v2regdur":
        return "days"
    if v == "v2elloseat":
        return "seats"
    if v == "v2ex_hosw":
        return "share 0-1"
    return "as coded"


def missing_variables(columns: Iterable[str]) -> list[str]:
    cols = set(columns)
    wanted = list(INDEX_VARS) + [v for v, _ in INDICATORS]
    return [v for v in wanted if v not in cols]


def wanted_columns(columns: Iterable[str]) -> list[str]:
    cols = list(columns)
    s = {x.column for d in DATASETS for x in plan_series(cols, d)}
    return [c for c in cols if c in s or c in ID_COLUMNS]


def read_vdem(raw: bytes) -> RFrame:
    """The identifier columns and every selected column of ``vdem.RData`` (two streaming passes)."""
    try:
        names = atlas_rdata.read_rdata_frame(raw, "vdem", lambda n: False).all_columns
        want = set(wanted_columns(names))
        miss = [c for c in ID_COLUMNS if c not in names]
        if miss:
            raise _err(f"vdem data frame lacks identifier columns {miss}")
        return read_rdata_frame(raw, "vdem", lambda n: n in want)
    except atlas_rdata.RDataError as e:
        raise _err(f"vdem.RData: {e}") from e


# ----------------------------------------------------------------------------- QIDs
QidPeriods = dict[str, list[tuple[int, int, str]]]


@dataclass(frozen=True)
class QidIndex:
    """``registry/entities/vdem_qids.json`` loaded: per V-Dem unit, (from, to, qid) periods."""

    periods: QidPeriods  # country_text_id -> [(from_year, to_year, qid)], period QIDs first, unit QID last
    text_id_by_vdem_id: dict[str, str]
    other: dict[tuple[str, str], str]  # ("lied_countryn"|"bmr_ccode", key) -> qid


@lru_cache(maxsize=4)
def _qids_cached(path: str, mtime_ns: int) -> QidIndex:
    doc = json.loads(Path(path).read_text())
    periods: QidPeriods = {}
    ids: dict[str, str] = {}
    for u in doc.get("units", []):
        t = u["vdem_country_text_id"]
        ids[str(u["vdem_country_id"])] = t
        lst = [(int(p["from"]), int(p["to"]), p["qid"]) for p in u.get("periods", []) if p.get("qid")]
        if u.get("qid"):
            lst.append((int(u["years"][0]), int(u["years"][1]), u["qid"]))
        if lst:
            periods[t] = lst
    other = {}
    for o in doc.get("other_units", []):
        for field in ("lied_countryn", "bmr_ccode"):
            for k in o.get(field) or []:
                if o.get("qid"):
                    other[(field, str(k))] = o["qid"]
    return QidIndex(periods, ids, other)


def qid_index(path: Path | None = None) -> QidIndex:
    """The agent-mapped QID crosswalk (empty when the file is absent)."""
    p = path or QIDS_PATH
    if not p.exists():
        return QidIndex({}, {}, {})
    return _qids_cached(str(p), p.stat().st_mtime_ns)


def vdem_qids(path: Path | None = None) -> QidPeriods:
    """V-Dem ``country_text_id`` -> [(from_year, to_year, QID)] (agent-mapped; see :func:`qid_at`)."""
    return qid_index(path).periods


def qid_at(periods: Mapping[str, Sequence[tuple[int, int, str]]], unit: str, year: int) -> str | None:
    """QID of ``unit`` in ``year``: the first listed period containing the year (period before unit QID)."""
    for a, b, q in periods.get(unit, ()):
        if a <= year <= b:
            return q
    return None


# ----------------------------------------------------------------------------- frames
def _canon(obj: Mapping[str, Any]) -> str:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_frame(
    data: RFrame,
    dataset: str,
    *,
    raw_sha256: str,
    release: str,
    qids: Mapping[str, Sequence[tuple[int, int, str]]] | None = None,
    years: tuple[int, int] | None = None,
    units: Sequence[str] | None = None,
) -> pl.DataFrame:
    """Atlas frame (``ATLAS_SCHEMA``, ``iso3`` left null: filled by ``geo_scheme="vdem"``) for one dataset.

    ``years`` / ``units`` restrict the rows (used by the fixture tests); NA cells are omitted.
    """
    from reva.atlas.tables import ATLAS_SCHEMA

    qids = dict(qids or {})
    cols = data.columns
    text_id = cols["country_text_id"]
    hist = cols["histname"]
    if not isinstance(text_id, StrColumn) or not isinstance(hist, StrColumn):
        raise _err("country_text_id / histname are not character columns")
    year_txt = [column_text(cols["year"], i) for i in range(data.nrows)]
    rows_idx = [
        i
        for i in range(data.nrows)
        if year_txt[i] is not None
        and text_id.values[i]
        and (years is None or years[0] <= int(year_txt[i]) <= years[1])  # type: ignore[arg-type]
        and (units is None or text_id.values[i] in units)
    ]
    geo = [text_id.values[i] for i in rows_idx]
    yrs = [int(year_txt[i]) for i in rows_idx]  # type: ignore[arg-type]
    qid_by_row = [qid_at(qids, g, y) for g, y in zip(geo, yrs, strict=True)]
    flags_by_row = [_canon({"vdem_histname": hist.values[i] or "", "vdem_release": release}) for i in rows_idx]
    parts: list[pl.DataFrame] = []
    for s in plan_series(data.all_columns, dataset):
        col = cols.get(s.column)
        if col is None:
            raise _err(f"column {s.column} was planned but not read")
        vals = [column_text(col, i) for i in rows_idx]
        keep = [k for k, v in enumerate(vals) if v is not None and v != ""]
        if not keep:
            continue
        labels = CATEGORY_LABELS.get(s.base) if s.role == "cat" else None
        value: list[str | None]
        value_text: list[str | None]
        if s.role == "text":
            value = [None] * len(keep)
            value_text = [vals[k][:4000] for k in keep]  # type: ignore[index]
        else:
            value = [_norm_decimal(vals[k], s.column) for k in keep]  # type: ignore[arg-type]
            value_text = [labels.get(v) if labels else None for v in value]  # type: ignore[arg-type]
            if labels:
                unknown = sorted({v for v, t in zip(value, value_text, strict=True) if t is None})
                if unknown:
                    raise _err(f"{s.column}: codes {unknown[:5]} have no codebook label (codebook changed?)")
        parts.append(
            pl.DataFrame(
                {
                    "indicator": [s.column] * len(keep),
                    "provider_geo": [geo[k] for k in keep],
                    "entity_qid": [qid_by_row[k] for k in keep],
                    "year": [yrs[k] for k in keep],
                    "value": value,
                    "value_text": value_text,
                    "unit": [s.unit] * len(keep),
                    "flags": [flags_by_row[k] for k in keep],
                    "row": [rows_idx[k] + 1 for k in keep],
                },
                schema={
                    "indicator": pl.String,
                    "provider_geo": pl.String,
                    "entity_qid": pl.String,
                    "year": pl.Int32,
                    "value": pl.String,
                    "value_text": pl.String,
                    "unit": pl.String,
                    "flags": pl.String,
                    "row": pl.Int64,
                },
            )
        )
    if not parts:
        return pl.DataFrame(schema=ATLAS_SCHEMA)
    f = pl.concat(parts, how="vertical")
    sel_prefix = '{"column":"'
    return f.select(
        pl.lit(SOURCE).alias("source"),
        pl.lit(dataset).alias("dataset"),
        pl.col("indicator"),
        pl.lit(None, dtype=pl.String).alias("concept_id"),
        pl.lit(None, dtype=pl.String).alias("iso3"),
        pl.col("provider_geo"),
        pl.col("entity_qid"),
        pl.date(pl.col("year"), 1, 1).alias("period_start"),
        pl.date(pl.col("year") + 1, 1, 1).alias("period_end"),
        pl.col("value"),
        pl.col("value_text"),
        pl.col("unit"),
        pl.col("flags"),
        pl.lit(raw_sha256).alias("raw_sha256"),
        pl.concat_str(
            [
                pl.lit(sel_prefix),
                pl.col("indicator"),
                pl.lit('","file":"vdem.RData","object":"vdem","row":'),
                pl.col("row").cast(pl.String),
                pl.lit("}"),
            ]
        ).alias("selector"),
    ).cast(dict(ATLAS_SCHEMA))  # type: ignore[arg-type]


_DEC = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][-+]?[0-9]{1,3})?$")


def _norm_decimal(v: str, column: str) -> str:
    s = v
    if "e" in s or "E" in s:
        mant, _, exp = s.partition("e") if "e" in s else s.partition("E")
        s = f"{mant}e{int(exp)}"
    if not _DEC.match(s):
        raise _err(f"{column}: value {v!r} is not a decimal")
    return s


# ----------------------------------------------------------------------------- ingest
def raw_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def coverage_note(dataset: str, release: str, data: RFrame) -> str:
    return (
        f"V-Dem Country-Year Full+Others v{release} (vdemdata R package, V-Dem Institute), {data.nrows} "
        f"country-years, 202 V-Dem units (26 historical states and the Palestinian West Bank/Gaza units "
        f"unmapped, kept; Taiwan = TWN, 1945-1948 flagged by histname). Dataset '{dataset}': V-Dem-produced variables only (no e_* third-party "
        "variables). NA cells omitted; election variables in election years only (not filled forward). "
        "Licence CC BY-SA 4.0."
    )


async def atlas_ingest(run: Any) -> dict[str, Any]:
    """Fetch the three raw files through ``run``, archive them and register the V-Dem atlas tables."""
    raws = {
        it.key: await run.get(it.url, key=it.key, media_types=it.expected_media_types, max_bytes=it.max_bytes)
        for it in fetch_items()
    }
    desc = parse_description(raws["vdemdata.description"].body)
    release = dataset_version(desc)
    if int(release.split(".")[0]) < EXPECTED_MAJOR_VERSION:
        raise _err(f"vdemdata version {release} is older than v{EXPECTED_MAJOR_VERSION}", kind="stale_release")
    parse_codebook(raws["vdemdata.codebook"].body)  # the archived codebook must be readable
    shas = {k: run.archive(r) for k, r in raws.items()}
    data = read_vdem(raws["vdemdata.vdem"].body)
    qids = vdem_qids()
    out: dict[str, Any] = {"release": release, "missing_variables": missing_variables(data.all_columns), "tables": {}}
    for ds in DATASETS:
        if not run.wants(ds):
            continue
        frame = build_frame(data, ds, raw_sha256=shas["vdemdata.vdem"], release=release, qids=qids)
        reg = run.register(
            dataset=ds,
            frame=frame,
            raw_sha256s=[shas["vdemdata.vdem"]],
            coverage_note=coverage_note(ds, release, data),
            geo_scheme=GEO_SCHEME,
            rights={
                **RIGHTS,
                "codebook_raw_sha256": shas["vdemdata.codebook"],
                "description_raw_sha256": shas["vdemdata.description"],
                "release": release,
            },
        )
        out["tables"][ds] = {
            k: reg[k] for k in ("dataset_key", "artifact_sha256", "row_count", "created", "unmapped_provider_geo")
        }
        del frame
    return out


__all__ = [
    "CATEGORY_LABELS",
    "CONNECTOR_NAME",
    "DATASETS",
    "INDEX_VARS",
    "INDICATORS",
    "RIGHTS",
    "SOURCE",
    "atlas_ingest",
    "atlas_source",
    "build_frame",
    "fetch",
    "fetch_all",
    "fetch_items",
    "missing_variables",
    "parse_codebook",
    "parse_description",
    "plan_series",
    "read_vdem",
    "vdem_qids",
]
