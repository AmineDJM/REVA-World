/**
 * Crude exposure ScenarioSpec v1 construction and client-side validation
 * (mirrors contracts/scenario.schema.json; the API remains authoritative).
 */
import type {
  BaselineReleaseRef,
  CountryCode,
  ElasticityPoint,
  Fidelity,
  ScenarioOutput,
  ScenarioSpec,
  SimResult,
  SimulationStatus,
} from '../api/types';
import { SIMULATION_BANNER_TEXT } from '../components/ContextBanner';
import { describeUncertainty } from '../components/UncertaintyPanel';
import { missingReasonLabel } from './format';

/** The placeholder baseline id in contracts/scenario.example.json. Never a real release. */
export const FIXTURE_BASELINE_UUID = '00000000-0000-4000-8000-000000000001';
const NIL_UUID = '00000000-0000-0000-0000-000000000000';
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const ALL_OUTPUTS: { key: ScenarioOutput; label: string }[] = [
  { key: 'import_value_change_usd', label: 'Import value change (US$)' },
  { key: 'export_value_change_usd', label: 'Export value change (US$)' },
  { key: 'net_external_cost_change_usd', label: 'Net external cost change (US$)' },
  { key: 'exposure_percent_baseline_gdp', label: 'Exposure (% of baseline GDP)' },
];

export function outputLabel(key: string): string {
  return ALL_OUTPUTS.find((o) => o.key === key)?.label ?? key.replace(/_/g, ' ');
}

export const IMPORT_ELASTICITY_RANGE: [number, number] = [-2, 1];
export const EXPORT_ELASTICITY_RANGE: [number, number] = [-1, 2];

export interface GridRow {
  importElasticity: string;
  exportElasticity: string;
}

export interface ScenarioForm {
  title: string;
  countries: CountryCode[];
  referenceYear: string;
  importMultiplier: string;
  exportMultiplier: string;
  fidelity: Extract<Fidelity, 'L1' | 'L2'>;
  grid: GridRow[];
  outputs: ScenarioOutput[];
  seed: string;
}

export const DEFAULT_FORM: ScenarioForm = {
  title: 'Crude border-price stress (+25%)',
  countries: ['FRA', 'CHN', 'USA'],
  referenceYear: '2023',
  importMultiplier: '1.25',
  exportMultiplier: '1.25',
  fidelity: 'L1',
  grid: [
    { importElasticity: '0', exportElasticity: '0' },
    { importElasticity: '-0.1', exportElasticity: '0.1' },
    { importElasticity: '-0.3', exportElasticity: '0.3' },
  ],
  outputs: ALL_OUTPUTS.map((o) => o.key),
  seed: '0',
};

/** Reject anything that is not a real, published-looking baseline release id. */
export function validateBaselineReleaseId(id: string | null | undefined): string | null {
  if (!id) return 'No published baseline release is available. Publish a belief release first.';
  if (!UUID_RE.test(id)) return 'Baseline release id is not a UUID.';
  if (id.toLowerCase() === FIXTURE_BASELINE_UUID) {
    return 'The example fixture baseline UUID is not a real release. Resolve a published release from the API.';
  }
  if (id === NIL_UUID) return 'The nil UUID is not a release.';
  return null;
}

function num(s: string): number | null {
  if (!/^[+-]?(\d+(\.\d*)?|\.\d+)$/.test(s.trim())) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

export interface FormErrors {
  [field: string]: string;
}

export function validateForm(form: ScenarioForm): FormErrors {
  const e: FormErrors = {};
  if (!form.title.trim()) e.title = 'Title is required.';
  else if (form.title.length > 200) e.title = 'Title is limited to 200 characters.';
  if (form.countries.length === 0) e.countries = 'Select at least one country.';
  const y = num(form.referenceYear);
  if (y === null || !Number.isInteger(y) || y < 2020 || y > 2024) e.referenceYear = 'Reference year must be 2020–2024.';
  for (const [k, label] of [
    ['importMultiplier', 'Import'],
    ['exportMultiplier', 'Export'],
  ] as const) {
    const v = num(form[k]);
    if (v === null || v <= 0 || v > 3) e[k] = `${label} multiplier must be a number in (0, 3].`;
  }
  if (form.outputs.length === 0) e.outputs = 'Select at least one output.';
  const seed = num(form.seed);
  if (seed === null || !Number.isInteger(seed) || seed < 0 || seed > Number.MAX_SAFE_INTEGER) e.seed = 'Seed must be a non-negative integer.';
  if (form.fidelity === 'L2') {
    if (form.grid.length === 0) e.grid = 'L2 needs at least one elasticity grid point.';
    if (form.grid.length > 100) e.grid = 'The elasticity grid is limited to 100 points.';
    form.grid.forEach((g, i) => {
      const im = num(g.importElasticity);
      const ex = num(g.exportElasticity);
      if (im === null || im < IMPORT_ELASTICITY_RANGE[0] || im > IMPORT_ELASTICITY_RANGE[1])
        e[`grid.${i}.import`] = `Import elasticity must be in [${IMPORT_ELASTICITY_RANGE.join(', ')}].`;
      if (ex === null || ex < EXPORT_ELASTICITY_RANGE[0] || ex > EXPORT_ELASTICITY_RANGE[1])
        e[`grid.${i}.export`] = `Export elasticity must be in [${EXPORT_ELASTICITY_RANGE.join(', ')}].`;
    });
  }
  return e;
}

export function buildScenarioSpec(form: ScenarioForm, release: Pick<BaselineReleaseRef, 'id' | 'knowledge_cutoff'>): ScenarioSpec {
  const releaseError = validateBaselineReleaseId(release.id);
  if (releaseError) throw new Error(releaseError);
  const grid: ElasticityPoint[] =
    form.fidelity === 'L2'
      ? form.grid.map((g) => ({
          import_elasticity: Number(g.importElasticity),
          export_elasticity: Number(g.exportElasticity),
          basis: 'scenario_assumption',
        }))
      : [];
  return {
    schema_version: '1.0.0',
    title: form.title.trim(),
    baseline_release_id: release.id,
    knowledge_cutoff: release.knowledge_cutoff,
    model_key: 'economic.crude_exposure',
    model_version: '1.0.0',
    countries: [...form.countries],
    reference_year: Number(form.referenceYear),
    horizon: { periods: 1, unit: 'annual_accounting_period' },
    intervention: {
      kind: 'set_exogenous_border_price_multiplier',
      import_multiplier: Number(form.importMultiplier),
      export_multiplier: Number(form.exportMultiplier),
      unit: 'ratio',
    },
    assumptions: {
      quantity_response: form.fidelity === 'L2' ? 'elasticity_sensitivity' : 'fixed',
      elasticity_grid: grid,
      no_general_equilibrium: true,
    },
    fidelity: form.fidelity,
    missing_input_policy: 'fail',
    outputs: ALL_OUTPUTS.map((o) => o.key).filter((k) => form.outputs.includes(k)),
    seed: Number(form.seed),
  };
}

// ------------------------------------------------------------------ exports

export interface ExportContext {
  simulation_id: string;
  scenario_id: string | null;
  scenario_title: string | null;
  baseline_release_id: string | null;
  baseline_release_sequence: number | null;
  knowledge_cutoff: string | null;
  model_versions: string[];
  fidelity: string | null;
  assumptions: string[];
  origin: string | null;
}

export function exportContext(simId: string, status: SimulationStatus, modelVersions: string[], assumptions: string[]): ExportContext {
  const sc = status.scenario;
  return {
    simulation_id: simId,
    scenario_id: sc?.id ?? null,
    scenario_title: sc?.title ?? null,
    baseline_release_id: sc?.baseline_release.id ?? null,
    baseline_release_sequence: sc?.baseline_release.sequence ?? null,
    knowledge_cutoff: sc?.knowledge_cutoff ?? sc?.baseline_release.knowledge_cutoff ?? null,
    model_versions: modelVersions,
    fidelity: status.run?.fidelity ?? null,
    assumptions,
    origin: sc?.origin ?? null,
  };
}

function resultValue(r: SimResult): { value: string; unit: string; missing: string } {
  const v = r.value;
  if (v.kind === 'decimal') return { value: String(v.value), unit: String(v.unit_ref), missing: '' };
  if (v.kind === 'float') return { value: String(v.value), unit: String(v.unit_ref), missing: '' };
  if (v.kind === 'missing') return { value: '', unit: '', missing: missingReasonLabel(String(v.reason)) };
  return { value: '', unit: '', missing: `unsupported value kind ${v.kind}` };
}

function csvCell(s: string): string {
  const safe = /^[=+\-@\t\r]/.test(s) && !/^-?\d/.test(s) ? `'${s}` : s; // spreadsheet formula guard
  return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

/** CSV with the simulation banner and full scenario context as leading comment lines. */
export function resultsToCsv(ctx: ExportContext, results: SimResult[]): string {
  const lines = [
    `# ${SIMULATION_BANNER_TEXT}`,
    `# simulation_id: ${ctx.simulation_id}`,
    `# scenario_id: ${ctx.scenario_id ?? 'unknown'}`,
    `# scenario_title: ${ctx.scenario_title ?? ''}`,
    `# baseline_release_id: ${ctx.baseline_release_id ?? 'unknown'}`,
    `# baseline_release_sequence: ${ctx.baseline_release_sequence ?? 'unknown'}`,
    `# knowledge_cutoff: ${ctx.knowledge_cutoff ?? 'unknown'}`,
    `# model_versions: ${ctx.model_versions.join('; ') || 'unknown'}`,
    `# fidelity: ${ctx.fidelity ?? 'unknown'}`,
    `# origin: ${ctx.origin ?? 'unknown'}`,
    ...ctx.assumptions.map((a) => `# assumption: ${a.replace(/[\r\n]+/g, ' ')}`),
    ['country', 'output_key', 'variant', 'value', 'unit_ref', 'missing_reason', 'uncertainty', 'lower', 'upper'].join(','),
  ];
  for (const r of results) {
    const v = resultValue(r);
    const u = describeUncertainty(r.uncertainty);
    lines.push(
      [
        r.subject?.label ?? String(r.applicability?.country ?? ''),
        r.output_key,
        r.variant,
        v.value,
        v.unit,
        v.missing,
        u.title,
        r.uncertainty?.lower ?? '',
        r.uncertainty?.upper ?? '',
      ]
        .map((c) => csvCell(String(c)))
        .join(','),
    );
  }
  return lines.join('\n') + '\n';
}

export function resultsToJson(ctx: ExportContext, results: SimResult[]): string {
  return JSON.stringify(
    {
      banner: SIMULATION_BANNER_TEXT,
      modality: 'assumption_conditioned_scenario',
      context: ctx,
      results: results.map((r) => ({
        country: r.subject?.label ?? r.applicability?.country ?? null,
        output_key: r.output_key,
        variant: r.variant,
        value: r.value,
        uncertainty: { ...r.uncertainty, label: describeUncertainty(r.uncertainty).title },
        result_id: r.id,
      })),
    },
    null,
    2,
  );
}
