# REVA World V3 — handoff / resume guide (2026-10-16, end of session)

Everything written by the lead and the agents is committed and pushed on `claude/kind-darwin-kyy8re`
(277+ commits). This file says where things stand and exactly how to resume.

## What is NOT in git (container-local, may be lost when the session container is reclaimed)

* **Dev database `reva`** (PostgreSQL 17 in docker container `reva-db`, ~0.8 GB, migration head
  `0016_causal_ops`): live atlas snapshot (159 tables, 87.5 M rows from 59 sources), KG statements, links
  store, causal claims loaded so far.
* **Archived raw provider bytes** `var/objects` (~3.8 GB) and the Wikidata archive `var/kg/objects`.
* **Throwaway DBs** (`reva_test_*`, `reva_kg_wdharvest`, `reva_kg_causwd`, `reva_causcand`, …).

**Latest data snapshot on GitHub (2026-10-17, after the full dev-DB load — atlas snapshot 1d9b950f…, 220 tables / 93.5 M rows, 1.15 M links, 27,791 causal claims):** branches `reva-data-20261017-b1` … `-b12` (`PARTS.txt` on b1 says which branch holds which part; dumps are split — concatenate before `pg_restore`; restore steps in `RESTORE.md`). Earlier snapshot, kept for reference:

**Saved on GitHub (2026-10-16):** branches `reva-data-20261016-b1` … `-b4` hold pg_dumps of `reva`,
`reva_kg_wdharvest`, `reva_kg_causwd`, `reva_causcand`, a tar of `var/objects` + `var/kg` (95 MB parts) and the
agents' scratch scripts; restore steps in `RESTORE.md` and checksums in `MANIFEST.sha256` on branch `-b1`.
Clone the code with `--single-branch` to avoid downloading the 4.3 GB of data.

All of it is also **reproducible from the committed code** (connectors archive raw bytes and rebuild tables
deterministically). Rebuilding costs provider requests within the daily budgets (3 GiB/day cap): roughly
2–3 budget days for the full atlas, longer for Wikidata (see below).

## Resume checklist (new session / new container)

```bash
dockerd > /tmp/dockerd.log 2>&1 &         # docker daemon is not started automatically
docker start reva-db || make up           # PostgreSQL 17 + PostGIS
uv sync && (cd ui && npm ci)
uv run reva db bootstrap                  # migrations to head (0017), roles, seed
uv run reva atlas list                    # every atlas source (discovered from atlas_*.py)
uv run reva atlas ingest <source>         # one at a time, budget-metered; then:
uv run reva atlas snapshot --skip-unchanged
uv run reva links materialise
uv run reva links prune --dry-run          # then without --dry-run: keeps the 2 newest + served/pinned ones
uv run reva causal refresh
uv run python -m reva.kg.harvest status   # Wikidata harvest state (resumable per spec)
```

Disk: keep ≥ 5 GB free (`df -h /`); the atlas query-file build needs ~6.4 GB + temp space. Run big
ingests sequentially (several sources peak at 3–5 GB RAM).

## State by layer (details: `docs/dev/v3_*.md`, 52 reports)

| Layer | State | Main docs |
|---|---|---|
| Atlas (all countries, ~9,250 concepts, 251 entities incl. TWN) | done; headline layer with source order, licences per cell, projections/source breaks flagged | `v3_atlascore.md`, `v3_atlasops.md`, `v3_a_*.md`, `v3_gaps.md` |
| Knowledge graph (Wikidata + datasets) | storage/API done; Wikidata harvest **throttled** (WDQS 429 for the whole session): countries + 442 sovereign states loaded in `reva_kg_wdharvest`; historical states run in progress; ~120 domain specs written and validated but not harvested | `v3_kgcore.md`, `v3_wdharvest.md`, `v3_kghist.md`, `v3_kgpeople.md`, `v3_kgcult.md`, `v3_kgscieco.md` |
| Dataset KG layers | done: Cliopatria polities + overlay, Glottolog/WALS/Grambank, COW, UCDP, UNGA votes, CShapes, UNTC treaties, UNESCO, Olympics, peacekeeping, cables/flights/space | `v3_kgpolity.md`, `v3_kglang.md`, `v3_kggeopol.md`, … |
| Links (country↔country) | done: store (0012/0013), 35+ link types, pair view, constrained paths (18/20 anchor chains) | `v3_links.md` |
| Events & causality | done: causal core (0015/0016), event backbone, T1 expert-coded claims (ICB, MID, UCDP, crises, contention, sanctions, hazards, coups ≈ 27k claims in throwaway runs), UI "Causes & conséquences", NLQA why/chain | `../roadmap/v3_causality.md`, `v3_causcore.md`, `v3_events.md`, `v3_causdata.md` |
| Simulations for all countries | cohort 1.1.0 (231 countries), retirement 1.1.0/1.2.0 (coverage-scaled) | `v3_simworld.md` |
| UI "Planète" | done: Neurones, Chemin, Frise, Atlas, Dossier, Demander, Pays↔Pays, Causes | `ui/src/v3/**` |
| Reviews | two independent reviews, all high/medium findings fixed | `review_v3.md`, `review_v3b.md` |

## Agents that were still running at handoff (their work so far is committed)

* **CAUSOPS** — loading event/causal sources into the dev DB, snapshot, links, causal refresh; result
  file `docs/dev/v3_causops_live.md` if it finished. Resume: re-run the ingest list in its brief.
* **CAUSWD** — Wikidata causal statements harvest (throwaway DB `reva_kg_causwd`).
* **CAUSCAND** — Wikipedia text candidates (quarantined), throwaway DB `reva_causcand`.
* **EVMAP** — typing all dataset events against real fixtures; sanctions / US programs / shared-hazard /
  colonial link types; ICOW↔COW independence `same_as`.
* Background processes: Wikidata core harvest (`reva.kg.harvest run registry/kg/queries/core --via api`)
  and ATLASOPS's delayed run of the sources refused by the daily byte cap (peacekeeping, gap sources,
  OpenAlex, RCS).

## Open technical items

1. **Wikidata harvest** is the biggest remaining gap (WDQS throttled; budget `wikidata.wdqs` 1,500/day,
   `wikidata.wbapi` 4,000/day). Run domain specs over several days: `uv run python -m reva.kg.harvest run
   registry/kg/queries/<domain> --via api` (or WDQS when it answers). Merge pending property requests:
   `uv run python -m reva.kg.harvest merge-property-requests` (events.yaml: P1923, P8031, P8032, P726,
   P991, P1590, P1339) when the budget resets.
2. `reva_kg_wdharvest` was migrated to an earlier 0015 revision → recreate it (or re-bootstrap) before reuse.
3. ~~Links store retention policy~~ **done (LINKRET, migration `0017_links_retention`)**: `uv run reva links prune
   [--dry-run] [--keep 2] [--vacuum plain|full]` (owner credentials) deletes the derived `kg.link_row` rows of
   materialisations nothing serves, pins or references; records `kg.link_prune` + `ops.audit` (`links.prune`);
   details in `docs/dev/v3_links.md` § LINKRET. A full materialisation is ~1.4 GB (not 400 MB); the dev DB holds
   one, so the first run pruned nothing. Run it after each `links_materialise` that wrote new rows (not a worker
   job: owner-only).
4. Final V3 completion report + `docs/implementation_status.md` V3 ledger rows (not yet written;
   `docs/final_report.md` covers V1/V2).
5. Full test sweep on a quiet machine (`uv run pytest -m "not live and not perf"`), full e2e
   (`uv run python scripts/e2e_stack.py --run --v3-fixtures`), UI vitest/build.
6. Cleanup of leftover throwaway databases (`reva_test_*`).

## Decisions only the owner can take

* **Credentials / accounts**: OpenAI and Gemini credits exhausted (NL drafts and LLM planner fall back to
  keywords); Comtrade subscription key; OpenAlex API key; Seshat account (CC BY-SA data behind terms);
  SIPRI authorisation (arms transfers/military expenditure); hosts blocking non-browser clients
  (Powell & Thyne coups, Cline Center, ILO NORMLEX, UNODA, CITES/Ramsar) — ask providers or allow-list.
* **Licence reviews** (flagged on every cell): non-commercial — IPU, CShapes, JST Macrohistory,
  Constitute/CCP; no-redistribution / UN terms — ICOW, UN Treaty Collection, OHCHR; unclear —
  WHR, OECD-WTO BaTIS, SPC Pacific, INFORM, Laeven-Valencia (© IMF), Reinhart-Rogoff, ICB, Archigos,
  COW (cite-only).
* **Reviews by a human maintainer**: retirement rule tables (163 WBL 2026 transcriptions), atlas source-order
  equivalences, manual country aliases, ATA/PSE Wikidata QIDs, causal claims (`kg.causal_review`, owner-only).
* **Privacy**: the session environment exposes the owner's e-mail to agents (`CLAUDE_CODE_USER_EMAIL`);
  two agents put it once in a User-Agent header (Wikidata, paulhensel.org). Removing that variable from the
  environment prevents recurrence.
