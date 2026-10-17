"""Effect estimates: the read side (plain dicts in the JSON shape of ``docs/dev/v5_effect_interface.md``).

Every estimate is shown with its source and an attributed wording ("According to …; REVA does not establish this
effect"). ``compare_estimates`` sets estimates side by side by comparability key and never pools them."""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from reva.effects.variables import Resolver, default_resolver

ESTIMATE_NOTE = (
    "Effect estimates are numbers attributed to their sources (publication, REVA estimation run or identity); "
    "REVA does not establish these effects. Transportability to another place, period or shock is not implied."
)
ESTIMATE_NOTE_FR = (
    "Les estimations d'effet sont des nombres attribués à leur source (publication, estimation REVA ou identité) ; "
    "REVA n'établit pas ces effets. Leur transposition à un autre lieu, une autre période ou un autre choc n'est pas "
    "impliquée."
)
COMPARE_NOTE = (
    "Side-by-side comparison only: estimates are grouped by comparability key (measure, unit, transformations, "
    "horizon, shock) and never pooled or averaged across groups; the descriptive statistics of a group are not a "
    "meta-analysis."
)
MAX_LIMIT = 500
COMPARE_KEY = (
    "measure",
    "unit",
    "cause_transform",
    "effect_transform",
    "horizon_kind",
    "horizon_value",
    "horizon_unit",
    "shock_size",
    "shock_unit",
)
MEASURE_WORDS = {
    "elasticity": ("elasticity", "élasticité"),
    "pass_through": ("pass-through", "transmission"),
    "semi_elasticity": ("semi-elasticity", "semi-élasticité"),
    "slope": ("slope", "pente"),
    "multiplier": ("multiplier", "multiplicateur"),
    "response": ("response", "réponse"),
    "identity_coefficient": ("identity coefficient", "coefficient d'identité"),
}
UNIT_WORDS = {
    "ratio": ("", ""),
    "percent_per_percent": (" % per 1 %", " % pour 1 %"),
    "percent_per_pp": (" % per percentage point", " % par point de pourcentage"),
    "percent_per_unit": (" % per unit", " % par unité"),
    "pp_per_pp": (" percentage point per percentage point", " point par point de pourcentage"),
    "pp_per_percent": (" percentage point per 1 %", " point de pourcentage pour 1 %"),
    "unit_per_unit": (" per unit", " par unité"),
    "currency_per_currency": (" per currency unit", " par unité monétaire"),
    "percent": (" %", " %"),
    "percentage_points": (" percentage point(s)", " point(s) de pourcentage"),
    "log_points": (" log point(s)", " point(s) log"),
}


def _f(x: Any) -> Any:
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, date):
        return x.isoformat()
    return x


def _ref(kind: str, id_: str, definition: str, transform: str, res: Resolver) -> dict[str, Any]:
    labels = res.labels(kind, id_)
    return {
        "kind": kind,
        "id": id_,
        "label": labels["en"] if labels else f"{id_} (label not loaded)",
        "label_fr": labels["fr"] if labels else f"{id_} (libellé non chargé)",
        "label_loaded": labels is not None,
        "definition": definition,
        "transform": transform,
    }


def _who(r: Mapping[str, Any]) -> tuple[str, str]:
    if r["source_kind"] == "publication":
        c = r.get("citation") or {}
        short = str(c.get("short") or c.get("text") or r["source_key"])
        loc = f" ({r['locator']})" if r.get("locator") else ""
        return f"{short}{loc}", f"{short}{loc}"
    if r["source_kind"] == "reva_run":
        run = r.get("run") or {}
        s = f"REVA estimation run {run.get('run_id')} (data as of {run.get('as_of')}, code {run.get('code_version')})"
        f = f"l'estimation REVA {run.get('run_id')} (données au {run.get('as_of')}, code {run.get('code_version')})"
        return s, f
    ident = (r.get("identity") or {}).get("name") or "identity"
    return f"the identity '{ident}'", f"l'identité « {ident} »"


def wording(r: Mapping[str, Any], cause: Mapping[str, Any], effect: Mapping[str, Any]) -> tuple[str, str]:
    who_en, who_fr = _who(r)
    m_en, m_fr = MEASURE_WORDS.get(r["measure"], (r["measure"], r["measure"]))
    u_en, u_fr = UNIT_WORDS.get(r["unit"], ("", ""))
    val = r.get("as_printed") or (None if r.get("point") is None else str(_f(r["point"])))
    if val is None:
        val_en, val_fr = f"of {r['sign']} sign", f"de signe {r['sign']}"
    else:
        val_en, val_fr = f"{val}{u_en}", f"{val}{u_fr}"
    hz = ""
    hz_fr = ""
    if r.get("horizon_value") is not None:
        hz = f", {r['horizon_kind']} at {_f(r['horizon_value'])} {r['horizon_unit']}(s)"
        hz_fr = f", {r['horizon_kind']} à {_f(r['horizon_value'])} {r['horizon_unit']}"
    elif r.get("horizon_kind") not in (None, "unspecified"):
        hz, hz_fr = f", {r['horizon_kind']}", f", {r['horizon_kind']}"
    if r["measure"] == "response":
        shock = f"{_f(r['shock_size'])} {r['shock_unit']}"
        en = (
            f"According to {who_en}, the estimated response of {effect['label']} to a shock of {shock} in "
            f"{cause['label']} is {val_en}{hz} (method: {r['method']}); REVA does not establish this effect."
        )
        fr = (
            f"Selon {who_fr}, la réponse estimée de {effect['label_fr']} à un choc de {shock} sur "
            f"{cause['label_fr']} est de {val_fr}{hz_fr} (méthode : {r['method']}) ; REVA n'établit pas cet effet."
        )
    else:
        en = (
            f"According to {who_en}, the estimated {m_en} of {effect['label']} with respect to {cause['label']} is "
            f"{val_en}{hz} (method: {r['method']}); REVA does not establish this effect."
        )
        fr = (
            f"Selon {who_fr}, la {m_fr} estimée de {effect['label_fr']} par rapport à {cause['label_fr']} est de "
            f"{val_fr}{hz_fr} (méthode : {r['method']}) ; REVA n'établit pas cet effet."
        )
    return en, fr


def estimate_json(r: Mapping[str, Any], res: Resolver | None = None) -> dict[str, Any]:
    res = res or default_resolver()
    cause = _ref(r["cause_kind"], r["cause_id"], r["cause_definition"], r["cause_transform"], res)
    effect = _ref(r["effect_kind"], r["effect_id"], r["effect_definition"], r["effect_transform"], res)
    en, fr = wording(r, cause, effect)
    out = {
        "estimate_key": r["estimate_key"],
        "row_id": r["id"],
        "cause": cause,
        "effect": effect,
        "measure": r["measure"],
        "unit": r["unit"],
        "shock": None if r.get("shock_size") is None else {"size": _f(r["shock_size"]), "unit": r["shock_unit"]},
        "shock_profile": (r.get("meta") or {}).get("shock_profile") or "unspecified",
        "sign": r["sign"],
        "point": _f(r.get("point")),
        "point_kind": r.get("point_kind"),
        "as_printed": r.get("as_printed"),
        "uncertainty": {
            "se": _f(r.get("se")),
            "ci_low": _f(r.get("ci_low")),
            "ci_high": _f(r.get("ci_high")),
            "ci_level": _f(r.get("ci_level")),
            "interval_kind": r.get("interval_kind"),
            "draws": r.get("draws"),
            "status": r["uncertainty_status"],
        },
        "horizon": {"kind": r["horizon_kind"], "value": _f(r.get("horizon_value")), "unit": r.get("horizon_unit")},
        "irf": list(r.get("irf") or []),
        "scope": dict(r.get("scope") or {}),
        "method": r["method"],
        "identification": r.get("identification"),
        "meta": r.get("meta"),
        "source": {
            "kind": r["source_kind"],
            "source_key": r["source_key"],
            "dataset": r["dataset"],
            "citation": r.get("citation"),
            "locator": r.get("locator"),
            "quote": r.get("quote"),
            "selector": r.get("selector"),
            "transcription": r["transcription"],
            "raw_sha256": r.get("raw_sha256"),
            "run": r.get("run"),
            "identity": r.get("identity"),
            "rights": r.get("rights"),
            "retrieved_at": r["retrieved_at"].isoformat() if r.get("retrieved_at") else None,
        },
        "as_of": _f(r["as_of"]),
        "applicability": r.get("applicability"),
        "source_caveats": r.get("source_caveats"),
        "causal_claim_keys": list(r.get("causal_claim_keys") or []),
        "curation": dict(r.get("curation") or {}),
        "wording": en,
        "wording_fr": fr,
        "origin": r["origin"],
        "recorded_at": r["recorded_at"].isoformat() if r.get("recorded_at") else None,
        "load_id": r["load_id"],
    }
    if r.get("matched_via"):
        out["matched_via"] = r["matched_via"]
    return out


def _origins(origin: str) -> list[str]:
    return ["live", "test_fixture"] if origin == "any" else [origin]


def _ids(id_: str | None, include_variables: bool, res: Resolver) -> dict[str, str] | None:
    """id -> how it matched (``direct`` or ``variable:<id>``)."""
    if id_ is None:
        return None
    out = {id_: "direct"}
    if include_variables:
        for vid in res.variables.by_atlas_concept(id_):
            out.setdefault(vid, f"variable:{vid}")
    return out


def _rows(
    conn: Connection,
    *,
    cause: str | None = None,
    effect: str | None = None,
    cause_kind: str | None = None,
    effect_kind: str | None = None,
    country: str | None = None,
    method: str | None = None,
    measure: str | None = None,
    source_kind: str | None = None,
    as_of_max: date | str | None = None,
    include_variables: bool = True,
    origin: str = "any",
    limit: int = 100,
    after_id: int | None = None,
    res: Resolver,
) -> list[dict[str, Any]]:
    cmap = _ids(cause, include_variables, res)
    emap = _ids(effect, include_variables, res)
    where = ["e.origin = ANY(:origins)"]
    p: dict[str, Any] = {"origins": _origins(origin), "lim": max(1, min(int(limit), MAX_LIMIT))}
    for col, val in (
        ("cause_kind", cause_kind),
        ("effect_kind", effect_kind),
        ("method", method),
        ("measure", measure),
        ("source_kind", source_kind),
    ):
        if val is not None:
            where.append(f"e.{col} = :{col}")
            p[col] = val
    if cmap is not None:
        where.append("e.cause_id = ANY(:cids)")
        p["cids"] = list(cmap)
    if emap is not None:
        where.append("e.effect_id = ANY(:eids)")
        p["eids"] = list(emap)
    if country is not None:
        where.append("(e.scope->'countries' ? :country OR e.scope->'countries' ? 'WLD')")
        p["country"] = country
    if as_of_max is not None:
        where.append("e.as_of <= CAST(:asof AS date)")
        p["asof"] = str(as_of_max)
    if after_id is not None:
        where.append("e.id < :after")
        p["after"] = int(after_id)
    rows = [
        dict(r)
        for r in conn.execute(
            text(f"""SELECT e.* FROM kg.effect_estimate_current e WHERE {" AND ".join(where)}
                     ORDER BY e.id DESC LIMIT :lim"""),
            p,
        ).mappings()
    ]
    for r in rows:
        via = []
        if cmap is not None and cmap.get(r["cause_id"], "direct") != "direct":
            via.append(cmap[r["cause_id"]])
        if emap is not None and emap.get(r["effect_id"], "direct") != "direct":
            via.append(emap[r["effect_id"]])
        if via:
            r["matched_via"] = ",".join(via)
    return rows


def list_estimates(conn: Connection, **kw: Any) -> list[dict[str, Any]]:
    res = kw.pop("resolver", None) or default_resolver()
    return [estimate_json(r, res) for r in _rows(conn, res=res, **kw)]


def get_estimate(
    conn: Connection, estimate_key: str, *, origin: str = "any", resolver: Resolver | None = None
) -> dict[str, Any] | None:
    res = resolver or default_resolver()
    r = (
        conn.execute(
            text("""SELECT e.* FROM kg.effect_estimate_current e
                     WHERE e.estimate_key = :k AND e.origin = ANY(:o) ORDER BY e.id DESC LIMIT 1"""),
            {"k": estimate_key, "o": _origins(origin)},
        )
        .mappings()
        .first()
    )
    return estimate_json(dict(r), res) if r else None


def compare_estimates(
    conn: Connection, *, cause: str, effect: str, limit: int = MAX_LIMIT, **kw: Any
) -> dict[str, Any]:
    res = kw.pop("resolver", None) or default_resolver()
    rows = _rows(conn, cause=cause, effect=effect, limit=limit, res=res, **kw)
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(tuple(_f(r.get(k)) for k in COMPARE_KEY), []).append(r)
    out_groups = []
    for key, rs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        pts = [float(r["point"]) for r in rs if r.get("point") is not None]
        out_groups.append(
            {
                "comparability_key": dict(zip(COMPARE_KEY, key, strict=True)),
                "n": len(rs),
                "n_with_point": len(pts),
                "min": min(pts) if pts else None,
                "max": max(pts) if pts else None,
                "median": statistics.median(pts) if pts else None,
                "signs": sorted({r["sign"] for r in rs}),
                "methods": sorted({r["method"] for r in rs}),
                "source_kinds": sorted({r["source_kind"] for r in rs}),
                "countries": sorted({c for r in rs for c in (r.get("scope") or {}).get("countries", [])}),
                "estimates": [estimate_json(r, res) for r in rs],
            }
        )
    return {
        "cause": cause,
        "effect": effect,
        "n_estimates": len(rows),
        "truncated": len(rows) >= min(int(limit), MAX_LIMIT),
        "groups": out_groups,
        "note": COMPARE_NOTE,
    }


def effect_variables(resolver: Resolver | None = None) -> list[dict[str, Any]]:
    res = resolver or default_resolver()
    return [v.as_dict() for v in sorted(res.variables.items.values(), key=lambda x: x.id)]


def counts(conn: Connection, *, origin: str = "any") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in ("source_kind", "method", "measure", "dataset"):
        out[col] = {
            str(k): int(n)
            for k, n in conn.execute(
                text(f"""SELECT {col}, count(*) FROM kg.effect_estimate_current
                          WHERE origin = ANY(:o) GROUP BY 1 ORDER BY 1"""),
                {"o": _origins(origin)},
            )
        }
    return out


def as_of_filter(estimates: Sequence[Mapping[str, Any]], as_of: date) -> list[Mapping[str, Any]]:
    """Keep the estimates knowable at ``as_of`` (as-of replays)."""
    return [e for e in estimates if date.fromisoformat(str(e["as_of"])) <= as_of]
