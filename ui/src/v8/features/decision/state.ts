/**
 * URL state of the decision studio — every scenario is a URL (pure, no React).
 *
 *   /v8/decision?ref=ecb_plus_50bp_2027q1                     a reference decision (contract: v8Links.decision(ref))
 *   /v8/decision?ref=ecb_plus_50bp_2027q1&m=25                 a variant of it (one field overridden)
 *   /v8/decision?i=policy_rate_change&m=25&u=bp&s=2027Q1&c=DEU,FRA&h=8   a composed decision
 *   …&b.ref=ecb_plus_50bp_2027q1                               scenario B for the comparison (same keys, "b." prefix)
 *   …&tab=propagation&v=<variable id>&e=<edge id>&g=DEU&p=2028Q4   the view (tab, selected variable / edge, …)
 *
 * `ref` may itself hold a whole state ("i=…&m=…"), so that v8Links.decision(stateRef(fields)) round-trips for a
 * composed decision (boards pin decisions by such a ref).
 */
import type { DecisionDoc } from '../../../v5/decision/api';
import type { InstrumentT, Schema, Templates } from './api';

export const FIELD_KEYS = ['ref', 'i', 'm', 'u', 's', 'd', 'h', 'c', 'ag', 'al', 'tg', 'tl', 'l'] as const;
export type FieldKey = (typeof FIELD_KEYS)[number];
export type ScenarioFields = Partial<Record<FieldKey, string>>;
export const VIEW_KEYS = ['tab', 'v', 'e', 'g', 'p', 'cmp'] as const;

export interface StudioState {
  a: ScenarioFields;
  b: ScenarioFields | null;
  draws: string | null;
  seed: string | null;
}

function readFields(params: URLSearchParams, prefix: string): ScenarioFields {
  const out: ScenarioFields = {};
  for (const k of FIELD_KEYS) {
    const v = params.get(prefix + k);
    if (v !== null && v !== '') out[k] = v;
  }
  if (out.ref && out.ref.includes('=')) {
    // a nested state ref (boards / v8Links.decision of a composed scenario): outer keys override it
    const nested = readFields(new URLSearchParams(out.ref), '');
    delete out.ref;
    return { ...nested, ...out };
  }
  return out;
}

export function parseState(params: URLSearchParams): StudioState {
  const b = readFields(params, 'b.');
  return {
    a: readFields(params, ''),
    b: Object.keys(b).length ? b : null,
    draws: params.get('n'),
    seed: params.get('seed'),
  };
}

function writeFields(out: URLSearchParams, f: ScenarioFields, prefix: string) {
  for (const k of FIELD_KEYS) {
    const v = f[k];
    if (v !== undefined && v !== '') out.set(prefix + k, v);
  }
}

/** New query string for a state, keeping the view parameters of `keep` (tab, selection). */
export function stateToParams(st: StudioState, keep?: URLSearchParams, keepView = true): URLSearchParams {
  const out = new URLSearchParams();
  writeFields(out, st.a, '');
  if (st.b) writeFields(out, st.b, 'b.');
  if (st.draws) out.set('n', st.draws);
  if (st.seed) out.set('seed', st.seed);
  if (keep && keepView) for (const k of VIEW_KEYS) {
    const v = keep.get(k);
    if (v) out.set(k, v);
  }
  return out;
}

export function isEmpty(f: ScenarioFields | null | undefined): boolean {
  return !f || (!f.ref && !f.i);
}

/** A ref that names this scenario in a link or a board pin: the reference id, or the encoded fields. */
export function stateRef(f: ScenarioFields): string {
  const keys = Object.keys(f).filter((k) => f[k as FieldKey]);
  if (keys.length === 1 && f.ref) return f.ref;
  const p = new URLSearchParams();
  writeFields(p, f, '');
  return p.toString();
}

// ------------------------------------------------------------------ fields <-> decision
export function fieldsFromDecision(d: DecisionDoc): ScenarioFields {
  return {
    i: d.instrument,
    m: d.magnitude.value,
    u: d.magnitude.unit,
    s: d.start,
    d: d.duration_quarters == null ? '' : String(d.duration_quarters),
    h: String(d.horizon_quarters),
    c: d.countries.join(','),
    ag: d.actor.geo ?? '',
    al: d.actor.label_fr,
    tg: d.target.geo ?? '',
    tl: d.target.label_fr,
    l: d.label_fr,
  };
}

/** Fields of `f` that differ from reference decision `ref` (the variant's overrides). */
export function overridesOf(f: ScenarioFields, ref: DecisionDoc): ScenarioFields {
  const base = fieldsFromDecision(ref);
  const out: ScenarioFields = {};
  for (const k of FIELD_KEYS) {
    if (k === 'ref') continue;
    const v = f[k];
    if (v !== undefined && v !== (base[k] ?? '')) out[k] = v;
  }
  return out;
}

function hash8(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(16).padStart(8, '0');
}

const FIELD_FR: Record<string, string> = {
  i: 'instrument',
  m: 'grandeur',
  u: 'unité',
  s: 'début',
  d: 'durée',
  h: 'horizon',
  c: 'pays',
  ag: 'pays acteur',
  al: 'acteur',
  tg: 'pays cible',
  tl: 'cible',
};

export function unitFr(ins: InstrumentT | undefined, u: string): string {
  return ins?.unit_labels_fr?.[u] ?? u;
}

export interface Resolved {
  /** Request body for the API (reference_id for an unchanged reference, else a full decision). */
  body: { reference_id?: string; decision?: Record<string, unknown> } | null;
  decision: DecisionDoc | null;
  instrument: InstrumentT | undefined;
  problems: string[];
  kind: 'reference' | 'variant' | 'composed' | 'empty';
  variantOf: DecisionDoc | null;
  changed: string[];
}

/** Turn URL fields into a decision, validated exactly as the server's schema (problems in French). */
export function resolveScenario(f: ScenarioFields, t: Templates): Resolved {
  const empty: Resolved = { body: null, decision: null, instrument: undefined, problems: [], kind: 'empty', variantOf: null, changed: [] };
  if (isEmpty(f)) return empty;
  let base: DecisionDoc | null = null;
  if (f.ref) {
    base = t.references.find((r) => r.id === f.ref)?.decision ?? null;
    if (!base) return { ...empty, problems: [`Décision de référence inconnue : « ${f.ref} ».`] };
  }
  const over = base ? overridesOf(f, base) : f;
  const changed = Object.keys(over).filter((k) => k !== 'l' && k !== 'ref');
  if (base && changed.length === 0) {
    return { body: { reference_id: base.id }, decision: base, instrument: t.instruments.find((i) => i.id === base!.instrument), problems: [], kind: 'reference', variantOf: null, changed: [] };
  }
  const merged: ScenarioFields = { ...(base ? fieldsFromDecision(base) : {}), ...over };
  const ins = t.instruments.find((i) => i.id === merged.i);
  const problems = validateFields(merged, ins, t.schema);
  if (problems.length) return { ...empty, problems, instrument: ins, kind: base ? 'variant' : 'composed', variantOf: base, changed };
  const countries = splitCountries(merged.c ?? '');
  const doc: DecisionDoc = {
    id: '',
    label_fr: '',
    actor: { ...(base && !over.al && !over.ag ? base.actor : {}), label_fr: (merged.al || 'Acteur').trim(), geo: merged.ag?.trim().toUpperCase() || null },
    instrument: merged.i!,
    target: { ...(base && !over.tl && !over.tg ? base.target : {}), label_fr: (merged.tl || 'Cible').trim(), geo: merged.tg?.trim().toUpperCase() || null },
    magnitude: { value: merged.m!.trim(), unit: merged.u! },
    start: merged.s!.trim(),
    duration_quarters: merged.d ? Number(merged.d) : null,
    horizon_quarters: Number(merged.h),
    countries,
  };
  const desc = base
    ? `${base.label_fr.replace(/\s*\(variante[^)]*\)$/, '')} (variante : ${changed.map((k) => `${FIELD_FR[k] ?? k} ${k === 'm' ? `${merged.m} ${unitFr(ins, merged.u ?? '')}` : (merged[k as FieldKey] ?? '') || '—'}`).join(', ')})`
    : `${doc.actor.label_fr} — ${ins?.label_fr ?? merged.i} : ${merged.m} ${unitFr(ins, merged.u ?? '')} dès ${merged.s}`;
  doc.label_fr = (merged.l && (!base || over.l) ? merged.l : desc).slice(0, t.schema.label_max);
  const idBase = base ? `${base.id}.variant-` : 'custom.';
  doc.id = `${idBase}${hash8(JSON.stringify(doc))}`.slice(0, 100);
  const body: Record<string, unknown> = { ...doc };
  if (base) {
    // keep the reference's analogue anchors and plugin options: a variant changes only what the URL says
    for (const k of ['anchors', 'anchor_labels', 'plugin_options', 'countries_note_fr']) {
      const v = (base as Record<string, unknown>)[k];
      if (v !== undefined && v !== null) body[k] = v;
    }
    body.plugin_options = adaptPluginOptions(body.plugin_options, changed);
  }
  return { body: { decision: body }, decision: doc, instrument: ins, problems: [], kind: base ? 'variant' : 'composed', variantOf: base, changed };
}

export function splitCountries(s: string): string[] {
  return s
    .split(/[\s,;]+/)
    .map((c) => c.trim().toUpperCase())
    .filter(Boolean);
}

export function validateFields(f: ScenarioFields, ins: InstrumentT | undefined, sc: Schema): string[] {
  const p: string[] = [];
  if (!ins) {
    p.push(f.i ? `Instrument inconnu : « ${f.i} ».` : 'Choisissez un instrument.');
    return p;
  }
  const mag = (f.m ?? '').trim();
  if (!new RegExp(sc.magnitude_pattern).test(mag)) p.push('La grandeur doit être un nombre décimal (ex. -2 ou 50).');
  if (!f.u || !ins.magnitude_units.includes(f.u)) p.push(`Unité attendue pour cet instrument : ${ins.magnitude_units.map((u) => unitFr(ins, u)).join(', ')}.`);
  if (ins.needs.occurrence && mag && Number(mag) !== 1) p.push('Cet instrument ne propage qu’une mesure complète : grandeur 1 (aucune estimation ne permet de mettre à l’échelle une interdiction partielle).');
  if (!new RegExp(sc.start_pattern).test((f.s ?? '').trim())) p.push('Le début est un trimestre de la forme 2027Q1.');
  const h = Number(f.h);
  if (!Number.isInteger(h) || h < sc.horizon_min || h > sc.horizon_max) p.push(`L’horizon va de ${sc.horizon_min} à ${sc.horizon_max} trimestres.`);
  if (f.d) {
    const d = Number(f.d);
    if (!Number.isInteger(d) || d < sc.duration_min || d > sc.duration_max) p.push(`La durée va de ${sc.duration_min} à ${sc.duration_max} trimestres (vide = maintenue sur tout l’horizon).`);
  }
  const iso = new RegExp(sc.iso3_pattern);
  const cs = splitCountries(f.c ?? '');
  for (const c of cs) if (!iso.test(c)) p.push(`« ${c} » n’est pas un code pays ISO 3166 à trois lettres.`);
  if (new Set(cs).size !== cs.length) p.push('Chaque pays ne doit figurer qu’une fois.');
  if (cs.length > sc.countries_max) p.push(`Au plus ${sc.countries_max} pays d’intérêt.`);
  if (ins.needs.countries && cs.length === 0) p.push('Cet instrument agit sur chaque pays d’intérêt : indiquez au moins un pays.');
  const ag = (f.ag ?? '').trim().toUpperCase();
  const tg = (f.tg ?? '').trim().toUpperCase();
  if (ins.needs.actor_geo && !ag) p.push('Cet instrument a besoin du pays de l’acteur (code ISO3).');
  if (ins.needs.target_geo && !tg) p.push('Cet instrument a besoin du pays visé (code ISO3).');
  if (ag && !iso.test(ag)) p.push('Le pays de l’acteur doit être un code ISO3.');
  if (tg && !iso.test(tg)) p.push('Le pays visé doit être un code ISO3.');
  if ((f.al ?? '').length > sc.party_label_max || (f.tl ?? '').length > sc.party_label_max) p.push('Libellé d’acteur ou de cible trop long.');
  return p;
}

export function withDraws(body: Resolved['body'], st: StudioState): Record<string, unknown> | null {
  if (!body) return null;
  const out: Record<string, unknown> = { ...body };
  if (st.draws && /^\d+$/.test(st.draws)) out.draws = Number(st.draws);
  if (st.seed && /^\d+$/.test(st.seed)) out.seed = Number(st.seed);
  return out;
}

/**
 * A reference's plugin options may state values derived from its own magnitude and start (France: first birth
 * cohort 1962 = 2027 − 65). When a variant changes the magnitude or the start, such a derived value is dropped so
 * the engine derives it again from the variant (and labels it as derived) instead of keeping a stale one.
 */
export function adaptPluginOptions(opts: unknown, changed: string[]): Record<string, unknown> | undefined {
  if (!opts || typeof opts !== 'object') return undefined;
  const out: Record<string, unknown> = {};
  for (const [model, o] of Object.entries(opts as Record<string, unknown>)) {
    if (o && typeof o === 'object' && (changed.includes('m') || changed.includes('s'))) {
      const { first_birth_year: _drop, ...rest } = o as Record<string, unknown>;
      void _drop;
      out[model] = rest;
    } else out[model] = o;
  }
  return out;
}
