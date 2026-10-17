# V4 V4CORE — universal relation families, family traversal, entity page, space-time helpers

## FOR V4 AGENTS: how to map your properties to a relation family (read this first)

Every entity-valued property you load into the KG (a Wikidata P-id or your namespaced predicate, e.g.
`chebi:has_part`, `globi:eats`, `gem:owned_by`) should map to **one** universal family so that it is
traversable by family (`GET /v1/kg/entities/{id}/families`, family-constrained `GET /v1/kg/families/path`,
the "Fiche" UI page).

1. **Do not edit** `registry/kg/relations_v4.yaml` (owner V4CORE: families + core Wikidata mappings).
2. Create **your own file** `registry/kg/relations_v4.d/<agent lowercase>.yaml` (file name = `agent` lowercased):

   ```yaml
   version: 1
   agent: V4CHEM                 # ^[A-Z][A-Z0-9_]{1,31}$, equals the file stem upper-cased
   note: optional free text      # <= 2000 chars
   mappings:
     - property: "chebi:has_part"      # Wikidata P-id or <ns>:<name> (KG pid pattern; ns >= 2 chars)
       family: has_part                # one of the families below
       direction: forward              # optional: forward (default) = "subject <family> object"; reverse = "object <family> subject"
       subtype: has_part               # optional snake_case: the source's own relation name, kept for display
       source: chebi                   # optional: source key / provider
       share: {qualifier: share, scale: fraction}   # optional, only for families with share_allowed (made_of, part_of, has_part, owns, owned_by)
       subject_kinds: [compound]       # optional display hints
       object_kinds: [chemical_element]
       note: "verbatim caveat"         # optional, <= 500 chars
   ```

3. Check: `uv run python -m reva.kg.families check` (and `uv run pytest tests/unit/test_kg_families.py`).
   Rules it enforces:
   * schema `registry/kg/relations_v4.schema.json` (`#/$defs/dropin`);
   * **one family per property** across all files (first come; a duplicate is an error — coordinate in your report);
   * a Wikidata P-id must be in `registry/kg/properties.yaml` or requested in `registry/kg/property_requests/*.yaml`;
     a namespaced predicate must match the KG pid pattern and be registered by your loader
     (`reva.kg.model.register_properties`) before statements use it;
   * **no causal property**: Wikidata P828/P1542/P1478/P1479/P1536/P1537 and any namespaced name containing
     `cause(s|d)`, `causal`, `effect`, `material_basis`, `risk_factor`, `induces`, `leads_to` are refused. Causal
     statements a source makes go through the causal loader as attributed claims (`docs/roadmap/v3_causality.md`).
   * **share**: read only from the qualifier you name (in `kg.statement.qualifiers`; a list of one
     `{amount, unit}` or a scalar). Absent or ambiguous = Missing (`null`), never zero. `scale`: `fraction`,
     `percent`, `per_mille`, `ppm` (normalised to a fraction) or `source_unit` (quoted as given).
4. Commit only your file (`git add registry/kg/relations_v4.d/<agent>.yaml`). The API reloads the registry on
   restart; nothing else to register.

Families (`causal: false` for all — a schema constant):

| family | fr label | inverse (fr) | paired with | share |
|---|---|---|---|---|
| `is_a` | est un(e) | a pour instance ou sous-classe | — | |
| `part_of` | fait partie de | comprend | `has_part` | yes |
| `has_part` | comprend | fait partie de | `part_of` | yes |
| `made_of` | est composé de | est un constituant de | — | yes |
| `located_in` | est situé(e) dans | contient (lieu) | — | |
| `occurs_during` | a lieu pendant | est la période de | — | |
| `produces` | produit | est produit par | `produced_by` | |
| `produced_by` | est produit par | produit | `produces` | |
| `owns` | possède ou contrôle | est possédé ou contrôlé par | `owned_by` | yes |
| `owned_by` | est possédé ou contrôlé par | possède ou contrôle | `owns` | yes |
| `supplies` | fournit | achète à | `buys_from` | |
| `buys_from` | achète à | fournit | `supplies` | |
| `trades_with` | commerce avec | (symmetric) | — | |
| `eats` | mange | est mangé par | — | |
| `interacts_with` | interagit avec | (symmetric) | — | |
| `associated_with` | est associé à | (symmetric) — association, never causation | — | |
| `measured_by` | est mesuré par | mesure | — | |
| `discovered_by` | a été découvert par | a découvert | — | |
| `invented_by` | a été inventé ou développé par | a inventé ou développé | — | |
| `regulates` | régule | est régulé par | — | |
| `context` | a pour contexte | est le contexte de | — | |

Python: `from reva.kg.families import load_registry, share_of` — `reg.edge_family(pid, "out"|"in")` gives the
family and fr/en label of an edge read from either end; `reg.properties_for({"made_of"})` gives the
`(pid, direction)` pairs to traverse.

(The rest of this report is completed as the work lands.)
