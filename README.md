# REVA World

REVA World V1 is a private, single-user MVP: a versioned register of official
statistics (evidence with revisions and provenance), release-pinned belief
estimates and one narrow scenario model. It is not a world model and makes no
causal, welfare or forecast claims about the economy.

What V1 runs:

* **Evidence**: World Bank WDI (population, nominal GDP), UN Comtrade public
  preview (HS 2709 crude values, connector 1.1.0), Natural Earth 1:110m (display
  geometry only), Crossref (DOI metadata only) and, via template-only autonomy,
  Eurostat `demo_pjan` (France). Missing source values stay typed `Missing`, never
  zero; e.g. Algeria has no Comtrade crude rows for 2020–2023.
* **Beliefs**: immutable releases selected by `source_select_v1`, with as-of
  reads of what REVA actually published.
* **Scenario**: `economic.crude_exposure` 1.0.0, a one-year border-value
  accounting stress test (L0 cache, L1 fixed quantities, L2 labelled elasticity
  sensitivity range with no probability level). L3/L4 and later phases P8–P12
  are interface-only and refuse with structured capability gaps.
* **Evaluation**: a revised-data retrospective forecast tournament (DM/HLN with
  Holm, locked holdout) and three labelled replay modes.

Read before relying on any output: [limitations](docs/limitations.md),
[methodology](docs/methodology.md), [scientific validation](docs/scientific_validation.md),
[threat model](docs/threat_model.md), [API](docs/api.md),
[operations](docs/operations.md), [deployment](docs/deployment.md).

Local development: `make help` lists the targets (`make up`, `make sync`,
`make bootstrap`, `make seed`, `make test`).
