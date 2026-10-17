"""Shared helpers of the chemistry & physics-of-matter connectors (V4, agent V4CHEM).

Used by :mod:`atlas_chem_elements` (PubChem periodic table + element pages: elements, nuclides),
:mod:`atlas_chem_chebi` (ChEBI ontology subset), :mod:`atlas_chem_pubchem` (PubChem compounds) and
:mod:`atlas_chem_codata` (NIST CODATA constants). Nothing here makes a network request.

* predicates: ``registry/kg/predicates_chem.yaml`` -> ``KgProperty`` (registered by each connector);
* :func:`load_kg`: bounded ``load_batch`` calls with retraction of statements a new build no longer has;
* decimals: KG quantities are plain decimal strings (no exponent, ``reva.kg.model.DECIMAL_RE``), atlas
  values strict decimals (exponent allowed); conversions are exact (``Decimal``), never through floats;
* :func:`parse_formula`: element symbol -> atom count of a molecular formula (symbols resolved through
  the periodic table the run fetched, never through names); :func:`mass_shares` computes mass fractions
  (``computed_by: reva``).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

import yaml

from reva.adapters.connectors.atlas_sci_common import atlas_frame, canon, code_digest, trim_registration
from reva.kernel.errors import SourceResponseError

REPO_ROOT = Path(__file__).resolve().parents[4]
PREDICATES_FILE = REPO_ROOT / "registry" / "kg" / "predicates_chem.yaml"
SEEDS_FILE = REPO_ROOT / "registry" / "chem" / "seeds.yaml"
PUBCHEM_HOST = "pubchem.ncbi.nlm.nih.gov"
PERIODIC_TABLE_URL = f"https://{PUBCHEM_HOST}/rest/pug/periodictable/JSON"
NOT_GEOGRAPHIC = "not_geographic"


class ChemParseError(SourceResponseError):
    pass


def err(msg: str, kind: str = "schema_drift") -> ChemParseError:
    return ChemParseError(msg, retryable=False, kind=kind)


# ============================================================================================== decimals
_NUM_RE = re.compile(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$")


def to_decimal(text: str | None) -> Decimal | None:
    """A provider number (spaces as digit-group separators removed) as an exact ``Decimal``; None if not a number."""
    if text is None:
        return None
    t = text.replace(" ", "").replace(" ", "").replace(" ", "").strip()
    if not t or not _NUM_RE.match(t):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def kg_decimal(d: Decimal) -> str:
    """Plain decimal text accepted by the KG (``[+-]?digits(.digits)?``, no exponent)."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    if s in ("-0", "+0"):
        s = "0"
    return s


def atlas_decimal(d: Decimal) -> str:
    """Strict atlas decimal (``reva.atlas.tables.DECIMAL_RE``): plain when short, else scientific."""
    plain = kg_decimal(d)
    if len(plain.replace("-", "").replace(".", "")) <= 30:
        neg = plain.startswith("-")
        body = plain[1:] if neg else plain
        ip, _, fp = body.partition(".")
        ip = ip.lstrip("0") or "0"
        return ("-" if neg else "") + ip + (("." + fp) if fp else "")
    sign, digits, exp = d.normalize().as_tuple()
    ds = "".join(map(str, digits))
    e = int(exp) + len(ds) - 1
    mant = ds[0] + ("." + ds[1:] if len(ds) > 1 else "")
    return ("-" if sign else "") + mant + f"e{e}"


def sig(d: Decimal, digits: int = 6) -> Decimal:
    """Round to ``digits`` significant digits (for REVA-computed shares only)."""
    if d == 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = digits
        return +d


# ============================================================================================== predicates / KG
def load_predicates(prefixes: Sequence[str] = (), path: Path = PREDICATES_FILE) -> list[Any]:
    """``KgProperty`` objects of ``predicates_chem.yaml`` (only pids starting with one of ``prefixes`` if given)."""
    from reva.kg.model import KgProperty

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = (
        "pid",
        "value_kind",
        "reva_predicate",
        "label_en",
        "label_fr",
        "inverse_name",
        "inverse_label_en",
        "inverse_label_fr",
    )
    out = []
    for p in doc["predicates"]:
        if prefixes and not any(p["pid"].startswith(x) for x in prefixes):
            continue
        out.append(KgProperty(**{k: p[k] for k in keys if k in p}))
    return out


def predicate_families(path: Path = PREDICATES_FILE) -> dict[str, str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {p["pid"]: p["family"] for p in doc["predicates"]}


def current_keys(engine: Any, source_key: str, origin: str, prefix: str) -> set[str]:
    from sqlalchemy import text

    like = f"{source_key}:{prefix}".replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with engine.connect() as c:
        return set(
            c.execute(
                text("""SELECT statement_key FROM kg.statement_current
                        WHERE source_key = :s AND origin = :o AND statement_key LIKE :p"""),
                {"s": source_key, "o": origin, "p": like},
            ).scalars()
        )


def load_kg(
    engine: Any,
    store: Any,
    *,
    entities: Sequence[Any],
    statements: Sequence[Any],
    source_key: str,
    raw_sha256s: Sequence[str],
    retrieved_at: datetime,
    origin: str,
    parameters: Mapping[str, Any],
    prefixes: Sequence[str],
    retract_prefix: str | None,
) -> dict[str, Any]:
    """Register the predicates, then load in bounded batches. Statements of this source whose key starts with
    ``<source_key>:<retract_prefix>`` and that the new build no longer has are retracted (``None``: no
    retraction, e.g. a partial run). A typed ``failed`` result (nothing written by that batch) on error."""
    from sqlalchemy.exc import DBAPIError

    from reva.kernel.errors import RevaError
    from reva.kg.model import (
        MAX_ENTITIES_PER_BATCH,
        MAX_RETRACTIONS_PER_BATCH,
        MAX_STATEMENTS_PER_BATCH,
        load_batch,
        register_properties,
        statement_key,
    )

    try:
        props = register_properties(engine, load_predicates(prefixes)).__dict__
        retract: list[str] = []
        if retract_prefix is not None:
            new = {statement_key(s, source_key) for s in statements}
            retract = sorted(current_keys(engine, source_key, origin, retract_prefix) - new)
        n = max(
            1,
            -(-len(statements) // MAX_STATEMENTS_PER_BATCH),
            -(-len(entities) // MAX_ENTITIES_PER_BATCH),
            -(-len(retract) // MAX_RETRACTIONS_PER_BATCH),
        )
        reports = []
        for i in range(n):
            rep = load_batch(
                engine,
                store,
                entities=entities[i * MAX_ENTITIES_PER_BATCH : (i + 1) * MAX_ENTITIES_PER_BATCH],
                statements=statements[i * MAX_STATEMENTS_PER_BATCH : (i + 1) * MAX_STATEMENTS_PER_BATCH],
                source_key=source_key,
                raw_sha256s=sorted(set(raw_sha256s)),
                retrieved_at=retrieved_at,
                origin=origin,
                retract_keys=retract[i * MAX_RETRACTIONS_PER_BATCH : (i + 1) * MAX_RETRACTIONS_PER_BATCH],
                parameters=dict(parameters),
            )
            reports.append(rep.as_dict())
    except RevaError as e:
        return {"status": "failed", "error": e.message, "details": getattr(e, "details", None)}
    except DBAPIError as e:
        return {"status": "failed", "error": f"{type(e.orig).__name__}: {str(e.orig)[:300]}"}
    keys = (
        "entities_inserted",
        "entities_unchanged",
        "labels_inserted",
        "statements_inserted",
        "statements_new_revision",
        "statements_unchanged",
        "statements_stale",
        "statements_conflicting",
        "retractions_inserted",
    )
    rejected: Counter[str] = Counter()
    sample: list[Any] = []
    for r in reports:
        rejected.update(r.get("rejected_counts") or {})
        sample.extend((r.get("rejected") or [])[: 20 - len(sample)])
    return {
        "status": "loaded",
        **{k: sum(int(r.get(k) or 0) for r in reports) for k in keys},
        "entities_built": len(entities),
        "statements_built": len(statements),
        "batches": len(reports),
        "load_ids": [r.get("load_id") for r in reports],
        "rejected_counts": dict(rejected),
        "rejected_sample": sample,
        "properties": props,
        "retracted_keys_sample": retract[:20],
    }


def quantity(amount: Decimal, unit: str | None = None, **extra: str) -> dict[str, Any]:
    v: dict[str, Any] = {"type": "quantity", "amount": kg_decimal(amount), "unit": unit}
    v.update(extra)
    return v


def string_value(s: str) -> dict[str, Any]:
    return {"type": "string", "value": s}


def text_value(s: str, lang: str = "en") -> dict[str, Any]:
    return {"type": "monolingualtext", "text": s, "language": lang}


# ============================================================================================== periodic table
@dataclass(frozen=True)
class Element:
    z: int
    symbol: str
    name: str
    cells: Mapping[str, str]  # PubChem column -> cell text (verbatim, "" when empty)
    row: int  # 0-based row index in Table.Row

    @property
    def key(self) -> str:
        return f"element:{self.z}"

    @property
    def atomic_mass(self) -> Decimal | None:
        return to_decimal(self.cells.get("AtomicMass"))


PT_COLUMNS = (
    "AtomicNumber",
    "Symbol",
    "Name",
    "AtomicMass",
    "CPKHexColor",
    "ElectronConfiguration",
    "Electronegativity",
    "AtomicRadius",
    "IonizationEnergy",
    "ElectronAffinity",
    "OxidationStates",
    "StandardState",
    "MeltingPoint",
    "BoilingPoint",
    "Density",
    "GroupBlock",
    "YearDiscovered",
)


def parse_periodic_table(body: bytes, *, min_elements: int = 118) -> list[Element]:
    """The 118 rows of PubChem's ``/rest/pug/periodictable/JSON`` (refused if columns drift or rows are missing)."""
    try:
        doc = json.loads(body)
        cols = doc["Table"]["Columns"]["Column"]
        rows = doc["Table"]["Row"]
    except (ValueError, KeyError, TypeError) as e:
        raise err(f"periodic table: unexpected JSON ({e})") from None
    if tuple(cols) != PT_COLUMNS:
        raise err(f"periodic table: columns changed: {cols}")
    out: list[Element] = []
    seen: set[int] = set()
    for i, r in enumerate(rows):
        cells = r.get("Cell") if isinstance(r, Mapping) else None
        if not isinstance(cells, list) or len(cells) != len(cols):
            raise err(f"periodic table: row {i} malformed")
        c = dict(zip(cols, (str(x) for x in cells), strict=True))
        if not c["AtomicNumber"].isdigit() or not re.match(r"^[A-Z][a-z]{0,2}$", c["Symbol"]):
            raise err(f"periodic table: row {i} has atomic number {c['AtomicNumber']!r} / symbol {c['Symbol']!r}")
        z = int(c["AtomicNumber"])
        if z in seen:
            raise err(f"periodic table: Z={z} twice")
        seen.add(z)
        out.append(Element(z, c["Symbol"], c["Name"], c, i))
    if len(out) < min_elements:
        raise err(f"periodic table: only {len(out)} elements", kind="incomplete")
    return out


# ============================================================================================== formulas
_TOKEN_RE = re.compile(r"([A-Z][a-z]?)([0-9]*)|(\()|(\))([0-9]*)")
_CHARGE_RE = re.compile(r"([0-9]*[+-])$")
_MULT_RE = re.compile(r"^([0-9]+)(?=[A-Z(])")


def parse_formula(formula: str, symbols: set[str]) -> tuple[dict[str, int] | None, str | None]:
    """Atom counts of a molecular formula (``C9H8O4``, ``CuSO4.5H2O``, ``C2H3O2-``, ``Ca(OH)2``).

    Returns ``(counts, None)`` or ``(None, reason)`` for formulas REVA does not interpret: R groups /
    unknown symbols, polymer ``n``, isotope labels ``[14C]``, wildcards. Charges are dropped (they do not
    change the atoms)."""
    f = formula.strip()
    if not f:
        return None, "empty"
    if any(ch in f for ch in "[]*nRX") and not _only_known(f, symbols):
        return None, "not_a_plain_formula"
    total: Counter[str] = Counter()
    for part in f.split("."):
        part = _CHARGE_RE.sub("", part.strip())
        mult = 1
        m = _MULT_RE.match(part)
        if m:
            mult = int(m.group(1))
            part = part[m.end() :]
        counts, why = _parse_part(part, symbols)
        if counts is None:
            return None, why
        for k, v in counts.items():
            total[k] += v * mult
    if not total:
        return None, "no_atoms"
    return dict(total), None


def _only_known(f: str, symbols: set[str]) -> bool:
    # "Rn", "Nb", "Xe" contain R/n/X legitimately: accept when every token is a known symbol and no brackets/stars
    if any(ch in f for ch in "[]*"):
        return False
    stripped = _CHARGE_RE.sub("", f.replace(".", ""))
    pos = 0
    while pos < len(stripped):
        m = re.match(r"[0-9]+|[()]|[A-Z][a-z]?", stripped[pos:])
        if not m:
            return False
        tok = m.group(0)
        if tok[0].isalpha() and tok not in symbols:
            return False
        pos += len(tok)
    return True


def _parse_part(part: str, symbols: set[str]) -> tuple[dict[str, int] | None, str | None]:
    stack: list[Counter[str]] = [Counter()]
    pos = 0
    while pos < len(part):
        m = _TOKEN_RE.match(part, pos)
        if not m or m.end() == pos:
            return None, "unparsed_token"
        sym, n, opn, cls, cn = m.groups()
        if sym:
            if sym not in symbols:
                return None, f"unknown_symbol:{sym}"
            stack[-1][sym] += int(n) if n else 1
        elif opn:
            stack.append(Counter())
        elif cls:
            if len(stack) == 1:
                return None, "unbalanced"
            inner = stack.pop()
            k = int(cn) if cn else 1
            for s, v in inner.items():
                stack[-1][s] += v * k
        pos = m.end()
    if len(stack) != 1:
        return None, "unbalanced"
    return dict(stack[0]), None


def mass_shares(counts: Mapping[str, int], masses: Mapping[str, Decimal]) -> dict[str, Decimal] | None:
    """Mass fraction of each element (6 significant digits), from atom counts and atomic masses; None when a
    mass is missing (never a guessed mass)."""
    parts: dict[str, Decimal] = {}
    for s, n in counts.items():
        m = masses.get(s)
        if m is None:
            return None
        parts[s] = m * n
    tot = sum(parts.values(), Decimal(0))
    if tot <= 0:
        return None
    return {s: sig(v / tot) for s, v in parts.items()}


def element_masses(elements: Sequence[Element]) -> dict[str, Decimal]:
    return {e.symbol: m for e in elements if (m := e.atomic_mass) is not None}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


__all__ = [
    "NOT_GEOGRAPHIC",
    "PERIODIC_TABLE_URL",
    "PUBCHEM_HOST",
    "Element",
    "atlas_decimal",
    "atlas_frame",
    "canon",
    "code_digest",
    "element_masses",
    "err",
    "kg_decimal",
    "load_kg",
    "load_predicates",
    "mass_shares",
    "parse_formula",
    "parse_periodic_table",
    "quantity",
    "sig",
    "slug",
    "string_value",
    "text_value",
    "to_decimal",
    "trim_registration",
]
