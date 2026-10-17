# V8 shell — design tokens and shared primitives

Owner: V8SHELL. Features (`ui/src/v8/features/<id>/`) import from here; they never edit it. Ask the lead (in
your report) for a new primitive or token.

```ts
import { Panel, Stat, Badge, MissingValue, SourceChip, atlasCellSource, KeyHint, Tabs, UrlTabs, TabPanel,
         EmptyState, ErrorState, Skeleton, VirtualTable, Sparkline, MiniLine, Split, useUrlParam,
         frDecimal, frValue, frNumber, frCompact, missingReasonFr } from '../../shell/ui';
import type { ColumnDef, HonestyKind, SourceInfo } from '../../shell/ui';
```

Importing `shell/ui` loads `tokens.css` and `primitives.css`, so a feature styled with `var(--v8-*)` works
standalone too. In vitest CSS is ignored (`css: false`).

## Tokens (`tokens.css`)

All tokens are CSS custom properties on `:root`, light by default, dark from `prefers-color-scheme`, forced with
`<html data-theme="light|dark">` (the top-bar toggle, or « Thème : … » in the palette).

| Group | Tokens |
|---|---|
| Surfaces / ink | `--v8-bg` (page), `--v8-surface` (panels), `--v8-surface-2`, `--v8-surface-3`, `--v8-border`, `--v8-border-strong`, `--v8-fg`, `--v8-fg-muted`, `--v8-fg-subtle`, `--v8-accent`, `--v8-accent-hover`, `--v8-accent-soft`, `--v8-fg-on-accent`, `--v8-focus`, `--v8-overlay` |
| Honesty (each has `-bg`) | `--v8-observed`, `--v8-projection`, `--v8-estimate`, `--v8-scenario`, `--v8-judged`, `--v8-missing`, `--v8-community`, `--v8-fixture`, `--v8-licence` |
| Missing hatch | `--v8-missing-hatch` (a `background` value: `background: var(--v8-missing-hatch), var(--v8-missing-bg)`), `--v8-missing-stripe` (stroke colour for SVG/map patterns) |
| Results | `--v8-win`, `--v8-loss`, `--v8-neutral` (+ `-bg`), `--v8-warn`, `--v8-ok`, `--v8-err` |
| Chart series | `--v8-cat-1` … `--v8-cat-6` (categorical; never use honesty colours for series identity) |
| Spacing | `--v8-space-1` 2 px, `-2` 4, `-3` 8, `-4` 12, `-5` 16, `-6` 24, `-7` 32, `-8` 48; `--v8-gutter` 16 px |
| Type | `--v8-font-sans`, `--v8-font-num` (use class `v8-num` for tabular figures), `--v8-font-mono`; sizes `--v8-text-2xs` 10.5, `-xs` 11.5, `-sm` 12.5, `-base` 13.5, `-md` 15, `-lg` 17, `-xl` 21, `-2xl` 28, `-3xl` 36; `--v8-leading`, `--v8-weight-medium/semibold` |
| Shape / depth / motion | `--v8-radius-sm` 4, `--v8-radius` 6, `--v8-radius-lg` 10, `--v8-radius-pill`; `--v8-shadow-sm/-/-lg`; `--v8-dur-fast` 90 ms, `--v8-dur` 160 ms, `--v8-dur-slow`, `--v8-ease` (all 0 with reduced motion) |
| Layout | `--v8-rail-w`, `--v8-topbar-h`, `--v8-row-h`, z-indexes `--v8-z-popover`, `--v8-z-modal` |

Charts (uPlot, maplibre, cytoscape) read tokens at draw time:
`getComputedStyle(document.documentElement).getPropertyValue('--v8-observed')`. Listen for theme changes with the
`v8:theme` window event (dispatched by the toggle) or a `prefers-color-scheme` media query.

Utility classes: `v8-num` (tabular figures), `v8-mono`, `v8-muted`, `v8-subtle`, `v8-sr-only`, `v8-grid`
(responsive card grid), `v8-row` (wrapping flex row), `v8-btn` (+ `v8-btn-primary`, `v8-btn-ghost`, `v8-btn-sm`),
`v8-input`.

## Page layout inside the shell

The shell renders your `feature.element` inside `<main class="v8-main">` with 16 px gutters (24 px on desktop), no
max width (dense dashboards may use the full width). Start your page with:

```tsx
<div className="v8-page">
  <header className="v8-page-head">
    <h1 className="v8-page-title">Monde</h1>
    <p className="v8-page-lede">Toute variable de l’atlas, par pays et par année.</p>
  </header>
  …
</div>
```

`v8-page-head` / `v8-page-title` / `v8-page-lede` are styled by the shell. Never cause horizontal scroll at 375 px:
use `minmax(0, 1fr)` grid tracks, `min-width: 0` on flex children, and `overflow-x: auto` only *inside* a table box.

## Primitives

### Panel
```tsx
<Panel title="Séries clés" subtitle="meilleure source disponible" actions={<button className="v8-btn v8-btn-sm">Comparer</button>}>
  …
</Panel>
<Panel title="Valeurs" flush>{/* edge-to-edge table */}<VirtualTable … /></Panel>
```
Props: `title`, `subtitle`, `actions`, `footer`, `dense`, `flush`, `id`, `as`.

### Stat — answer first
```tsx
<Stat label="PIB" value={cell.value /* "2.9e12" exact string or number, null = missing */} unit="US$ courants"
      period="2024" source={atlasCellSource(cell)} />
<Stat label="Inflation 2027" value="2.1" unit="%" kind="projection" source={{ source: 'IMF WEO', vintage: 'oct. 2026' }} />
<Stat label="Population" value={null} missing={{ reason: 'not_reported' }} />   // hatched "manquant : non rapporté…"
```
`kind` (`observed` default — not badged unless `showObserved`; `projection`/`scenario` numbers are tinted and
badged), `size` (`sm|md|lg`), `sub` (delta, rank…), `maxFractionDigits`, `format`. Exact decimal strings are never
converted to floats.

### Badge — honesty labels
```tsx
<Badge kind="observed" />            // observé
<Badge kind="projection" />          // projection (dashed dot)
<Badge kind="estimate" />            // estimation
<Badge kind="scenario" />            // scénario non validé
<Badge kind="judged">prévision jugée · ne bat pas le FMI</Badge>
<Badge kind="missing" />             // manquant (hatched)
<Badge kind="win">bat le FMI</Badge> <Badge kind="loss">perd vs SPF</Badge> <Badge kind="neutral">égalité</Badge>
<Badge kind="community" /> <Badge kind="fixture" /> <Badge kind="licence" />
```
Each kind has a default French tooltip; pass `title` to override. `HONESTY_LABEL` maps kinds to labels.

### MissingValue
```tsx
<MissingValue reason="not_reported" />                      // manquant : non rapporté par la source
<MissingValue reason="Pas de série ADM1 pour ce pays" />    // free French text is shown as is
<MissingValue reason={flags.missing} compact />             // table cells: "manquant", reason in tooltip
<MissingValue reason="…" block />                           // a hatched block (chart area without data)
```
Never render 0, "—" or a blank for a missing value. Maps/charts: fill missing areas with a pattern using
`--v8-missing-stripe`.

### SourceChip — every number with its source and licence
```tsx
<SourceChip source="worldbank_wdi/WDI" label="WDI" licence={cell.rights} period="2024" />
<SourceChip {...atlasCellSource(cell)} />                      // atlas cell → source, licence, period, raw sha, selector
<SourceChip source="wikidata" citation={citeKgStatement(st)} />  // v3 citations: details + community flag
<SourceChip source="IMF WEO" licence="IMF terms" vintage="2026-10" retrievedAt={iso} href="https://…" />
```
The chip shows `NC` for non-commercial licences (`lic.` for other restrictions), `comm.` for community sources,
`test` for fixtures. Hover/focus opens the popover; tap pins it (phone). `required_notice` texts are shown verbatim.
For a full licence list under an answer, reuse `LicenceList` from `ui/src/v3/components/Licence`.

### KeyHint
```tsx
<KeyHint keys="Mod K" />   // ⌘ K on mac, Ctrl K elsewhere
<KeyHint keys="g m" />     <KeyHint keys={['Shift', 'Enter']} />
```

### Tabs / UrlTabs / TabPanel — one URL per state
```tsx
<UrlTabs param="tab" label="Sections de la fiche" idBase="fiche"
         tabs={[{ id: 'faits', label: 'Faits' }, { id: 'liens', label: 'Liens', count: 42 }]} />
{tab === 'faits' && <TabPanel id="faits" idBase="fiche">…</TabPanel>}
```
`useUrlParam('tab', 'faits')` reads/writes the same parameter. Plain `Tabs` is controlled (`value`, `onChange`).
Arrow keys / Home / End move between tabs.

### useUrlParam
```ts
const [year, setYear] = useUrlParam('year', '2024');   // ?year=…; default value is not written
setYear('2025');                     // replace (typing, sliders)
setYear('2025', { replace: false }); // push a history entry (discrete navigation)
```

### EmptyState / ErrorState / Skeleton
```tsx
<EmptyState title="Aucune prévision sur ce pays">
  Le registre prospectif ne contient pas encore de question résolue sur la France.
</EmptyState>
<ErrorState error={err} what="la fiche" onRetry={reload} />   // 404 route → « Pas encore disponible », etc.
<Skeleton height={240} />   // fixed footprint: no layout shift when data arrives
<Skeleton lines={4} />
```

### VirtualTable
```tsx
const columns: ColumnDef<Row>[] = [
  { header: 'Pays', accessorKey: 'label' },
  { header: 'Valeur', accessorFn: (r) => toNumber(r.value), meta: { numeric: true },
    cell: ({ row }) => row.original.value === null ? <MissingValue reason={row.original.reason} compact /> : frValue(row.original.value, unit) },
  { header: 'Source', cell: ({ row }) => <SourceChip {...atlasCellSource(row.original.cell)} />, enableSorting: false },
];
<VirtualTable data={rows} columns={columns} caption="Valeurs par pays" height={480}
              getRowHref={(r) => v8Links.entity('country', r.iso3)} initialSorting={[{ id: 'Valeur', desc: true }]} />
```
Rows are virtualised (thousands OK), header sticky, `meta.numeric` right-aligns with tabular figures, rows with
`getRowHref` are links (Enter, click; Ctrl/⌘-click → new tab). Sort missing values last by returning `null` from
`accessorFn` and setting `sortUndefined: 'last'` / `sortingFn` as needed.

### Sparkline / MiniLine
```tsx
<Sparkline points={dossierConcept.series} latestSeries={dossierConcept.latest.series} />   // v3, atlas points
<MiniLine points={[{ x: 2020, y: 1.2 }, { x: 2021, y: null }, { x: 2022, y: 1.6 }]} />    // null = gap
<MiniLine points={pts} split={2026} />            // from 2026 on: dashed, projection colour
<MiniLine points={pts} kind="scenario" />
```

### Split
```tsx
<Split first={<List />} second={<Detail />} initial={0.4} storageKey="liens" />
```
Drag or keyboard (←/→, Shift for bigger steps) on the separator; stacks below 720 px.

### Formatting
`frDecimal("57082396439.6", 1)` → `57 082 396 439,6` (exact, no float); `frValue(value, unit)` picks digits by unit;
`frNumber(0.1234)`, `frCompact(2.9e12)` → `2,9 T`; `missingReasonFr('not_reported')`.

## Shell services available to features

- **Palette**: export `commands` (`PaletteCommand[]`) and `search` (`PaletteProvider`) on your `feature`; the shell
  debounces (120 ms), aborts stale queries (honour the `signal`), groups hits by `group`, and records recents.
  The default provider already covers entities (`/v1/v8/search`, or `/v1/kg/search` + atlas catalogue).
- **Go-shortcut**: `goKey: 'm'` → `g m` jumps to your feature (shown in the `?` sheet).
- **Breadcrumbs**: built from the URL (`/v8/fiche/country/FRA` → Console › Fiche › country › FRA). To give a
  readable last crumb, set `document.title = 'France · Fiche'`; the shell shows the first part of the title as the
  last crumb when it differs from the raw segment.
- **Deep links**: always build URLs with `v8Links` from `ui/src/v8/contract.ts`.
- **Theme**: `document.documentElement.dataset.theme` is `light`, `dark` or absent (system); `window`
  dispatches `v8:theme` on change.
