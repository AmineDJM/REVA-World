# R7 property check: kernel, crude exposure / simulation, belief selection

Hypothesis property tests (Hypothesis `>=6.100` is already a dev dependency; lock has 6.169.1).

| File | Scope | Result |
|---|---|---|
| `tests/property/test_prop_r7_kernel.py` | canonical hashing `reva-cjson-2`, Value union/Missing, ScalarDecimal/`decimal_str`, UncertaintySpec, TimeExtent, `external_id` | 25 passed, 3 xfailed |
| `tests/property/test_prop_r7_simulation.py` | `models/economic/crude_exposure.py`, `simulation/engine.compute` + `output_digest` over a hand-built `CompiledScenario` (no DB), `compile.build_coupling`/`compile_intervention`, `simulation/rng.py` | 14 passed, 2 xfailed |
| `tests/property/test_prop_r7_belief_selection.py` | `belief/selection.select_report` (pure) and `eligible_latest` (SQL, `db` fixture = fresh per-session test DB, never `reva`) | 4 passed, 1 xfailed |

Run: `uv run pytest tests/property/test_prop_r7_*.py -q` (about 45 s wall time including the ~8 s test-DB setup;
the pure files run in about 25 s). `max_examples` is between 40 and 400 for the pure tests and 6 (plus 2 explicit
examples) for the DB test.

Each property the code currently violates is an `xfail(strict=True)` test with the minimal counterexample
pinned as `@example`. Once the code is fixed the test XPASSes, which fails the run, so the marker must then be
removed.

## Failing properties (bugs), with minimal examples and proposed patches

Nothing under `src/` was edited. The patches below are proposals for the code owners.

### R7-K1 (kernel, low): aware datetimes at the calendar edge raise `OverflowError` — FIXED (kernel/ids.py)
`canonical_bytes(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5))))` raises
`OverflowError: date value out of range` from `astimezone(UTC)`. The same happens for `9999-12-31T23:00-05:00`.
Every other unsupported input raises `ValueError`/`TypeError`, so callers that map `ValueError` to a 422 answer
with a 500 here. Proposed patch for `src/reva/kernel/ids.py` `_normalise`:
```python
    if isinstance(obj, datetime):
        if obj.tzinfo is None or obj.utcoffset() is None:
            raise ValueError("naive datetimes are forbidden in canonical JSON")
        try:
            utc = obj.astimezone(UTC)
        except OverflowError as e:
            raise ValueError("datetime instant is outside the representable UTC range") from e
        return {"$datetime": utc.isoformat()}
```

### R7-K3 (kernel, low/medium): `UncertaintySpec.lower/upper` accept non-plain decimal strings
`UncertaintySpec(status="quantified", kind="bounds", description="d", method_version="m", lower=" 1", upper="999999")`
validates. So do `"1_0"`, `"1e5"`, `"+1"` and `"01"`, because `Decimal()` is lenient. `ScalarDecimal` and the
observation contract allow only `^-?(0|[1-9][0-9]*)(\.[0-9]+)?$`, so the same quantity has two string grammars, and
`" 1"` is stored verbatim. Proposed patch for `src/reva/kernel/uncertainty.py` `_dec` (`values.py` does not import
`uncertainty.py`, so there is no import cycle):
```python
from reva.kernel.values import _DECIMAL_RE
...
    if v is None:
        return None
    if not _DECIMAL_RE.fullmatch(v):
        raise ValueError(f"{name} must be a plain decimal string (no exponent, '+', whitespace or '_')")
```
Before merging, check stored rows: engine-produced bounds use `dstr` (plain) and already pass.

### R7-K4 (kernel, medium): `TimeExtent` does not order uncertain bounds against the other endpoint
These impossible extents validate:
- `TimeExtent(kind="interval", precision="day", start=None, start_uncertainty=UncertainBound(earliest=2020-01-01, latest=2020-01-01), end=2019-01-01)`:
  the end lies before every admissible start. This is the pinned minimal example.
- `TimeExtent(kind="interval", precision="day", start=2020-01-01, end_uncertainty=UncertainBound(earliest=2010-01-01, latest=2011-01-01))`:
  every admissible end lies before the start.
- `TimeExtent(kind="instant", ..., start=2020-01-01, end_uncertainty=...)`: an instant carries an end bound. This
  was found by a probe and is not a test.

Proposed patch for `src/reva/kernel/time.py` `TimeExtent._check`, appended before `return self`:
```python
if self.kind == "instant" and self.end_uncertainty is not None:
    raise ValueError("instant must not have an end bound")
earliest_start = (
    self.start
    if self.start is not None
    else (self.start_uncertainty.earliest if self.start_uncertainty is not None else None)
)
latest_end = (
    self.end if self.end is not None else (self.end_uncertainty.latest if self.end_uncertainty is not None else None)
)
if earliest_start is not None and latest_end is not None and latest_end <= earliest_start:
    raise ValueError("interval end must be after start (half-open), including uncertain bounds")
```

### R7-S1 (crude exposure, low): `exposure_l1`/`exposure_l2` map a kernel `Missing` (or `None`) to `validation_error`
`ce.exposure_l1(Missing(reason="not_collected"), Decimal(5), Decimal(100), 1.1, 1.1)` raises
`ValidationProblem("M must be numeric (got Missing)")` (code `validation_error`) instead of `MissingInput` (code
`missing_input`). The call never yields a number or zero. The typed path (`validate_country_inputs` /
`evaluate_country` / `engine` → `missing_input`) is correct. Proposed patch for
`src/reva/models/economic/crude_exposure.py` `_dec`, at the top (both names are already imported):
```python
    if x is None or isinstance(x, Missing):
        reason = x.reason.value if isinstance(x, Missing) else MissingReason.unavailable.value
        raise MissingInput(f"{what} is missing ({reason})", input=what, reason=reason)
```

### R7-S2 (simulation rng, low): stream names are not injective
`rng.stream_key(0, "a\x1fb", "c", 0) == rng.stream_key(0, "a", "b\x1fc", 0)`: both are `256eb7f8…85d2`.
`_name_words` hashes `f"{model_id}\x1f{entity_id}"` without escaping, so two distinct
(model, entity) names share one random stream, which breaks the "independent named streams" claim. V1 uses no
random draws, so the impact is latent. Proposed patch for `src/reva/simulation/rng.py`. It leaves names without a
backslash or U+001F unchanged, so existing seed plans keep their stream keys:
```python
def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\x1f", "\\u001f")


def _name_words(model_id: str, entity_id: str) -> tuple[int, ...]:
    digest = hashlib.sha256(f"{_esc(model_id)}\x1f{_esc(entity_id)}".encode()).digest()
```

### R7-B1 (belief selection, low): retracted-lineage output order depends on input row order
With one current lineage `src.a/a:1` plus retracted lineages `src.a/a:2` and `src.b/b:1` (preferences
`[src.a, src.b]`), reversing the input rows reverses `sel.alternatives[1:]` and the
`retracted_lineage_excluded:*` flags. The selected observation, value, uncertainty and the *sets* of alternatives
and flags are order-invariant (that property passes). Only the list order leaks into the stored estimate and the
release manifest bytes. `eligible_latest` orders its SQL output, so a release is reproducible today. Any caller
that passes rows in another order (replay, a cutoff repository) gets different bytes. Proposed patch for
`src/reva/belief/selection.py` `select_report`:
```python
        retracted = sorted(
            (c for c in group if c.get("selection_status") == "retracted" and c["source_key"] in prefs),
            key=lambda c: (prefs.index(c["source_key"]), c["logical_key"], str(c.get("origin", ""))),
        )
```

## Properties that hold

- **Hashing (`reva-cjson-2`):**
  - Hashes are deterministic and independent of key order, including nested key order.
  - The encoding is injective on mixed nested JSON-like data with `$`-prefixed keys, tags-as-keys, decimals,
    floats, dates, aware datetimes and UUIDs. This was checked by an independent decoder round-trip, and distinct
    objects give distinct hashes. A user mapping `{"$decimal": "1"}` or `{"$$decimal": …}` never collides with
    the tagged scalar.
  - Equal instants under different offsets hash equal, and different instants hash differently.
  - Naive datetimes are rejected anywhere in the tree, including under a `$date` key.
- **Values:** every Value union member, including `Missing` with every reason, round-trips
  `model_dump(mode="json")` → JSON → `parse_value` with equal value, type and `logical_hash`. `require_number`
  on `Missing` raises `MissingInput`.
- **ScalarDecimal:**
  - It accepts exactly the plain-decimal grammar: `+1`, `01`, `1.`, `.5`, `1e5`, `1_0`, `" 1"`, `"1\n"`, `٣`
    and `--1` are rejected; `-0` is accepted.
  - `decimal_str` output always passes the regex and round-trips (`Decimal(s) == d`). It is idempotent, has no
    trailing fractional zeros and never emits `-0`.
- **UncertaintySpec:**
  - Acceptance matches the documented rules exactly on the full status × kind × level × bounds × method grid.
  - `lower ≤ upper` holds whenever both bounds are present.
  - A sensitivity range never carries a level and is never `quantifies_measurement`.
- **TimeExtent:**
  - Intervals are half-open (accepted iff `end > start`), consecutive calendar years tile exactly, and an instant
    never has an `end`.
  - A known start or end must lie in its own uncertain bound.
- **`external_id`:**
  - `entity_id("FRA")` is pinned to `uuid5(REVA_NAMESPACE, "reva.entity\x1fFRA\x1f1")`, version 5.
  - It is deterministic, and ordinary keys keep the plain `\x1f` join.
  - It is injective over components that contain `\x1f`, `\` and literal `\u001f`.
- **Crude exposure (pure):**
  - ε = (0, 0), given as `0`, `Decimal("0.0")`, `-0`, `0.0`, `"0"` or `"0.000"`, gives an L2 result bit-identical
    (`as_tuple`) to L1.
  - Doubling `(multiplier − 1)` doubles ΔM, ΔX and Δnet exactly; the percentage matches within one 50-digit ulp.
  - The sign convention holds: Δnet = ΔM − ΔX, an import-only price rise gives a positive net cost, and an
    export-only price rise gives a negative one.
  - All outputs are `Decimal`, including for float inputs; `as_json` emits plain decimal strings.
  - 30-digit × 16-digit products keep every digit, and results do not depend on the caller's thread decimal
    context (precision 3 to 400).
- **Missing inputs:**
  - Any combination of `None`/`Missing` inputs gives `MissingInput` (code `missing_input`) listing exactly the
    missing inputs and their reasons, for L1 and L2.
  - `engine.compute` emits no row at all for a missing-input country.
- **Engine (no DB):**
  - Rows, `output_digest`, coupling schedule, `baseline_digest` and `seed_plan` do not depend on country order.
  - The L2 `sensitivity_range` row contains the L1 value and every grid value; its uncertainty is
    `sensitivity_range` with no level, and the L1 row stays `unquantified`.
  - There are no duplicate (output, country, variant) rows.
  - No float appears in any result `value`/`uncertainty`, and L1 strings reproduce the model's `Decimal` exactly.
- **RNG:** streams are deterministic by name and do not depend on the creation of other streams.
- **Belief selection, pure:**
  - The chosen observation, value, uncertainty and withheld set do not depend on candidate order.
  - Retracted rows are never selected and never carry their value forward.
  - The chosen source is the best-ranked usable one, and `superseded_revision_selected` is flagged exactly when
    applicable.
  - An estimand whose preferred lineages are all retracted yields no estimate and no withheld entry, never a stale
    fallback.
  - Retracting the selected head drops that lineage and flags it.
- **`eligible_latest` (DB):**
  - For 1 to 4 revisions and every intermediate cutoff, it selects the latest revision recorded at or before the
    cutoff and never a later one; a cutoff before the first revision yields nothing.
  - Retracting the head drops the lineage (`selection_status = retracted` only with `include_retracted`) without
    falling back to the superseded value. Retracting an older revision leaves the head selected.
  - A later retraction never changes an earlier as-of answer.

## Observations (not counted as failures)

- **Decimal canonical form keeps the representation only for negative exponents.**
  `logical_hash(Decimal("1E+2")) == logical_hash(Decimal("100"))`, but `Decimal("1.0")` and `Decimal("1")` hash
  differently, because `format(d, "f")` expands positive exponents and keeps trailing fractional zeros. This is a
  design decision. If numeric identity is intended, normalise with `decimal_str`; if representation identity is
  intended, encode the exponent.
- **L2 powers are not correctly rounded.** For a non-integral exponent `crude_exposure._power` computes
  `exp((1+ε)·ln p)`. For example `0.0001^(−0.5)` gives `100.0000…0001`: 1 ulp at 50 digits, so the exact case
  ΔM = 99 comes out as `99.000…0001`. The error is numerically negligible, but the module docstring says the
  result is "rounded correctly in that context", which overstates it: each step is correctly rounded, the
  composition is not.
- **Quantified interval specs need not carry their interval.** A kernel `UncertaintySpec` with
  `status=quantified, kind=confidence_interval|bounds` and neither `lower`/`upper` nor `artifact_sha256`
  validates. The observation contract requires `artifact_sha256` for quantified uncertainty; the kernel does not.
- **Lone surrogates.** `canonical_bytes("\ud800")` raises `UnicodeEncodeError`. That is a `ValueError` subclass,
  so it is acceptable.
- **Environment:** with the current uncommitted `migrations/versions/0007_security.py`,
  `retract_observation(..., actor="human:…")` on `db.worker` is refused (`governance: reva_worker may not record a
  human-attributed activity`). As a result
  `tests/integration/test_evidence_fixes.py::test_r27_retracted_latest_revision_is_not_replaced_by_superseded_value`
  currently errors. The R7 DB property records its retraction through `db.owner` (a reviewer path) and is not
  affected. The owners of the governance rule and that test need to reconcile the two.
- **Not covered:** the `eligible_latest` fallback from an ineligible head (connector-version eligibility), and
  multi-origin releases.
