"""A tiny SYNTHETIC inter-country table (3 economies x 2 industries) in FIGARO's SDMX-CSV layout.

Test data only: invented numbers chosen so the Leontief price solution can be computed by
hand. Economies FR, DE, WRL_REST; industries B, C10-12. Every gross output is 100 except
WRL_REST:C10-12, which has no output at all (all zeros).

Non-zero intermediate flows (supplier -> user):  DE:B -> FR:C10-12 = 20,
FR:C10-12 -> DE:C10-12 = 30, DE:C10-12 -> FR:C10-12 = 10.
Household final consumption of FR (P3_S14): FR:C10-12 = 50, DE:C10-12 = 30, DE:B = 20.
Each row's remaining output goes to its own economy's P51G; each column's remaining
input is compensation of employees D1.

Hand-computed solution for a +10 % price change of DE:B (π = 0.1), pass-through θ:
    p_FR_C = θ (0.2 π + 0.1 p_DE_C),  p_DE_C = θ · 0.3 · p_FR_C
    θ = 1:   p_FR_C = 0.02 / 0.97 = 2/97,          p_DE_C = 0.6/97
    θ = 0.5: p_FR_C = 0.01 / 0.9925,               p_DE_C = 0.15 · p_FR_C
FR basket (θ = 1) = (50·2/97 + 30·0.6/97 + 20·0.1) / 100 = 3.12/0.97 % = 3.216494845...%
"""

from __future__ import annotations

import gzip
from decimal import Decimal

COUNTRIES = ("FR", "DE", "WRL_REST")
INDUSTRIES = ("B", "C10-12")
FINAL = ("P3_S13", "P3_S14", "P3_S15", "P51G", "P5M")
PRIMARY = ("B2A3G", "D1", "D21X31", "D29X39", "OP_RES", "OP_NRES")
HEADER = "DATAFLOW,LAST UPDATE,freq,ind_use,ind_ava,c_dest,unit,c_orig,TIME_PERIOD,OBS_VALUE,OBS_FLAG,CONF_STATUS"
DATASET = "NAIO_10_FCP_II4"
YEAR = 2024

Z = {(("DE", "B"), ("FR", "C10-12")): Decimal(20),
     (("FR", "C10-12"), ("DE", "C10-12")): Decimal(30),
     (("DE", "C10-12"), ("FR", "C10-12")): Decimal(10)}
HH_FR = {("FR", "C10-12"): Decimal(50), ("DE", "C10-12"): Decimal(30), ("DE", "B"): Decimal(20)}
ZERO_OUTPUT = {("WRL_REST", "C10-12")}


def output(k: tuple[str, str]) -> Decimal:
    return Decimal(0) if k in ZERO_OUTPUT else Decimal(100)


def cells() -> dict[tuple[str, str, str, str], Decimal]:
    """All dense cells (c_orig, ind_ava, c_dest, ind_use) -> value."""
    out: dict[tuple[str, str, str, str], Decimal] = {}
    inds = [(c, i) for c in COUNTRIES for i in INDUSTRIES]
    for r in inds:
        used = Decimal(0)
        for c in inds:
            v = Z.get((r, c), Decimal(0))
            out[(r[0], r[1], c[0], c[1])] = v
            used += v
        for d in COUNTRIES:
            for f in FINAL:
                v = HH_FR.get(r, Decimal(0)) if (d, f) == ("FR", "P3_S14") else Decimal(0)
                out[(r[0], r[1], d, f)] = v
                used += v
        out[(r[0], r[1], r[0], "P51G")] += output(r) - used
    for c in inds:
        inputs = sum(Z.get((r, c), Decimal(0)) for r in inds)
        for p in PRIMARY:
            out[("DOM", p, c[0], c[1])] = (output(c) - inputs) if p == "D1" else Decimal(0)
    for d in COUNTRIES:
        for f in FINAL:
            for p in PRIMARY:
                out[("DOM", p, d, f)] = Decimal(0)
    return out


def body(origins: tuple[str, ...], overrides: dict[tuple[str, str, str, str], str] | None = None,
         drop: set[tuple[str, str, str, str]] | None = None, year: int = YEAR) -> bytes:
    """gzip SDMX-CSV response for the given supplying economies (or ("DOM",))."""
    rows = [HEADER]
    for k, v in sorted(cells().items()):
        if k[0] not in origins or (drop and k in drop):
            continue
        val = (overrides or {}).get(k, f"{v:.3f}")
        rows.append(f"ESTAT:{DATASET}(1.0),20/07/26 11:00:00,A,{k[3]},{k[1]},{k[2]},MIO_EUR,{k[0]},{year},{val},,")
    return gzip.compress(("\n".join(rows) + "\n").encode(), mtime=0)


def groups() -> list[tuple[str, ...]]:
    return [("FR", "DE"), ("WRL_REST",), ("DOM",)]


def constraint_xml(years: tuple[int, ...] = (2022, 2023, YEAR)) -> bytes:
    def kv(dim: str, values: list[str]) -> str:
        return f'<c:KeyValue id="{dim}">' + "".join(f"<c:Value>{v}</c:Value>" for v in values) + "</c:KeyValue>"

    parts = [kv("freq", ["A"]), kv("ind_use", [*INDUSTRIES, *FINAL]), kv("ind_ava", [*INDUSTRIES, *PRIMARY]),
             kv("c_dest", list(COUNTRIES)), kv("unit", ["MIO_EUR"]), kv("c_orig", [*COUNTRIES, "DOM"]),
             kv("TIME_PERIOD", [str(y) for y in years])]
    return ('<?xml version="1.0" encoding="UTF-8"?><m:Structure xmlns:m="m" xmlns:c="c"><c:CubeRegion>'
            + "".join(parts) + "</c:CubeRegion></m:Structure>").encode()
