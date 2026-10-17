# V3 AUDFIX: dataset and loader fixes for the causal audit

Agent AUDFIX, 2026-10-17. Scope: the dataset / loader-data side of `docs/dev/review_v3_causal_audit.md`
(H4 data part, M2–M7, L1–L5, L8). Grading rules and Wikidata tiering are CAUSFIX's; `same_as` and corroboration
are SAMEAS's.

## Interface for CAUSFIX: the `source_validity` claim qualifier (H4)

A dataset claim whose source codes the certainty or validity of the coded event or of the coded link carries the
key **`source_validity`** in `kg.causal_claim.scope` (`DatasetClaim.scope["source_validity"]`). It is a list
(one entry per coded certainty field), each entry verbatim from the source's own code list:

```json
{
  "source_validity": [
    {
      "field": "eventValidity",
      "code": "1",
      "label": "Very Doubtful Tsunami",
      "codelist": "NCEI descriptor tsunami/validity",
      "codelist_sha256": "081f620e…",
      "certainty": "doubtful"
    },
    {
      "field": "causeCode",
      "code": "2",
      "label": "Questionable Earthquake",
      "codelist": "NCEI descriptor tsunami/causes",
      "codelist_sha256": "ccb91b45…",
      "certainty": "doubtful"
    }
  ]
}
```

* `code` and `label` are the source's code and its label, copied from the archived code list (`codelist_sha256`
  is the archived response that holds the label).
* `certainty` is the registry reading of that code. Its values are `doubtful` and `definite_or_probable`. The
  code → certainty mapping is declared per source in the registry with the source's label (NOAA:
  `registry/causal/relations_datasets.yaml` → `source_validity.noaa_tsunami`). It is never inferred from text.
* A claim gets one entry per coded certainty field. A claim whose source codes no such field has no
  `source_validity` key (the absence means "not coded", not "certain").
* Grading hook for CAUSFIX: any entry with `certainty: doubtful` should lower the grade (e.g. a fact rule
  `certainty.doubtful`). The NOAA entries ship with the `atlas.noaa_hazards` connector version 1.1.0.

NOAA codes:

| field | code | NCEI label (descriptor) | certainty | claim |
|---|---|---|---|---|
| eventValidity | −1 | Erroneous entry | — | no claim (excluded, as before) |
| eventValidity | 0 | Event that only caused a seiche | doubtful | claim with qualifier |
| eventValidity | 1 | Very Doubtful Tsunami | doubtful | claim with qualifier |
| eventValidity | 2 | Questionable Tsunami | doubtful | claim with qualifier |
| eventValidity | 3 | Probable Tsunami | definite_or_probable | claim with qualifier |
| eventValidity | 4 | Definite Tsunami | definite_or_probable | claim with qualifier |
| causeCode | 2 | Questionable Earthquake | doubtful | claim with qualifier |
| causeCode | 1, 3–7 | (descriptor label) | definite_or_probable | claim with qualifier |

(Work in progress: the per-defect results follow below as each fix lands.)
