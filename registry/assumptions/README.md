# Literature assumptions (`registry/assumptions/`)

Curated by agent LIT. `review_status: maintainer_review_pending`: no human has reviewed these extractions.

These files hold **published numbers that V2 models may use as stated scenario assumptions with
citations** (docs/roadmap/v2_design.md, "Literature parameters"). They are not evidence claims, they
are not loaded into the belief store, and they never become a selected estimate.

| File | Consumer | Content |
|---|---|---|
| `retirement_employment_response.yaml` | PEN (`social.retirement_age`) | Employment and pension-receipt effects of raising the pension eligibility age (AUT, GBR, DEU, NLD, FRA, ITA, AUS, USA) and the derived share of people kept out of pension receipt who are employed instead |
| `io_energy_passthrough_benchmarks.yaml` | IO (`economic.io_price`) | Published input-output (and one macro-model) estimates of first-round price effects of energy/oil price increases, for plausibility checks only |
| `schema.json` | both | JSON Schema (draft 2020-12) for both files |

Rationale, the proposed sensitivity grid and the limits of transfer to France are in
`docs/dev/v2_literature.md`.

## Rules the files obey (enforced by `tests/unit/test_assumptions_registry.py`)

* Every number in `values` has `verification: verified_from_text`, a `locator` (table/row/column or
  section, with PDF page), a verbatim `quote` that contains the number `as_printed`, and a
  `text_source` whose SHA-256 and byte count were recorded at access time.
* `evidence: primary` means the number was read in a version of the cited work (published article,
  author manuscript or working paper; `kind` says which, and `differs_from_cited_version` warns when
  the version read is not the cited one). `evidence: secondary_compilation` means it was read in
  another paper that tabulates the cited work's number with a primary locator (here mostly IZA DP
  14150, Appendix Table B.1).
* An entry whose number could not be seen in an accessible text has `verification_status:
  metadata_only`, no `values`, and a `not_verified` reason.
* `derived` items are REVA computations (`sign * numerator / denominator`, rounded) from values of
  the same entry; the test recomputes them. `printed_as` is set only when the authors print the
  same ratio themselves.
* DOIs were checked against the Crossref works API (`metadata_check`, with the digest of the JSON
  received).
* Fetched texts are **not archived** in the repository (licensing unclear for most); only digests.
  Some sources are dynamic web pages, so a refetch may legitimately give different bytes.

## Units

`percentage_points` (change in a rate x 100), `proportion` (change in a rate as a fraction; 0.144 =
14.4 pp), `percent` (relative change of a price level, or a printed percentage), `share` (derived
ratio in [0, 1]), `years_per_year` (years of exit/claim age per year of eligibility age).

## Using a value

Cite the entry `id` and the value `name`, show the `citation` and `review_status` next to any output
that depends on it, and run the whole sensitivity grid rather than one point. Do not edit numbers
here without a fetched text and a new digest; do not add numbers from memory.
