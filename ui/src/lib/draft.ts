/**
 * Natural-language scenario drafts (POST /v1/scenario-drafts): normalisation of the server's
 * response and a structural JSON diff for the review step.
 *
 * The draft is untrusted until the user confirms it: the UI never runs it, never fills a
 * missing field with a guess, and shows every unsupported part as a capability gap.
 */
import type { DraftAssumption, DraftNumber, DraftUnsupportedItem, ScenarioDraft } from '../api/v2types';
import { isRecord } from './format';

function str(x: unknown): string | null {
  return typeof x === 'string' && x.trim() ? x : null;
}

function num(x: unknown): number | null {
  return typeof x === 'number' && Number.isFinite(x) ? x : null;
}

const records = (x: unknown): Record<string, unknown>[] => (Array.isArray(x) ? x.filter(isRecord) : []);

/**
 * Normalise a POST /v1/scenario-drafts response (ScenarioDraftResponse in
 * src/reva/api/routes/nl.py). Accepts the bare response or an envelope ({data}). Absent fields
 * stay empty or null; nothing is filled in.
 */
export function normalizeDraft(body: unknown): ScenarioDraft {
  const r = isRecord(body) && isRecord(body.data) ? body.data : isRecord(body) ? body : {};
  const unsupported: DraftUnsupportedItem[] = records(r.unsupported).map((u) => ({
    text: str(u.text),
    capability_key: str(u.capability_key),
    kind: str(u.kind),
    reason: str(u.reason),
    source: str(u.source),
    llm_capability_key_rejected: typeof u.llm_capability_key_rejected === 'boolean' ? u.llm_capability_key_rejected : null,
  }));
  const numbers: DraftNumber[] = records(r.numbers_to_confirm).map((n) => ({
    path: String(n.path ?? ''),
    value: n.value,
    provenance: str(n.provenance),
    quote: str(n.quote),
  }));
  const assumptions: DraftAssumption[] = Array.isArray(r.assumptions_added)
    ? r.assumptions_added.map((a) => (isRecord(a) ? { text: String(a.text ?? ''), source: str(a.source) } : { text: String(a) }))
    : [];
  return {
    status: str(r.status) ?? 'unknown',
    confirmation_required: r.confirmation_required !== false,
    executed: r.executed === true,
    draft_spec: isRecord(r.draft_spec) ? r.draft_spec : null,
    draft_spec_sha256: str(r.draft_spec_sha256),
    candidate_spec: isRecord(r.candidate_spec) ? r.candidate_spec : null,
    validation: isRecord(r.validation) ? r.validation : {},
    restatement: str(r.restatement) ?? '',
    language: str(r.language),
    llm_restatement: str(r.llm_restatement),
    unsupported,
    rejected_ids: records(r.rejected_ids),
    missing_fields: Array.isArray(r.missing_fields) ? r.missing_fields.map(String) : [],
    assumptions_added: assumptions,
    numbers_to_confirm: numbers,
    provider: str(r.provider),
    model: str(r.model),
    model_requested: str(r.model_requested),
    prompt_version: str(r.prompt_version),
    prompt_sha256: str(r.prompt_sha256),
    catalog_digest: str(r.catalog_digest),
    usage: isRecord(r.usage) ? r.usage : null,
    latency_ms: num(r.latency_ms),
    fallback_from: records(r.fallback_from),
    warnings: Array.isArray(r.warnings) ? r.warnings.map(String) : [],
    next_step: str(r.next_step),
  };
}

/** The spec the review form starts from: the valid draft, else the candidate (review only). */
export function reviewSpec(d: ScenarioDraft): Record<string, unknown> | null {
  return d.draft_spec ?? d.candidate_spec;
}

export const STATUS_TEXT: Record<string, string> = {
  draft_ready: 'Draft ready: valid scenario specification, waiting for your confirmation',
  needs_input: 'Needs input: the draft is incomplete; required fields are missing',
  draft_invalid: 'Invalid draft: the server validator rejected it',
  draft_rejected: 'Draft refused by the server guard',
  nothing_draftable: 'Nothing draftable: no part of the text maps to a supported model',
};

// ------------------------------------------------------------------ JSON diff

export interface DiffEntry {
  /** JSON-path-like location, e.g. "intervention.import_multiplier" or "countries[1]". */
  path: string;
  kind: 'added' | 'removed' | 'changed';
  before?: unknown;
  after?: unknown;
}

function join(base: string, key: string | number): string {
  return typeof key === 'number' ? `${base}[${key}]` : base ? `${base}.${key}` : key;
}

/** Structural diff from `a` to `b`; keys in sorted order so the output is deterministic. */
export function diffJson(a: unknown, b: unknown, path = ''): DiffEntry[] {
  if (Object.is(a, b)) return [];
  if (Array.isArray(a) && Array.isArray(b)) {
    const out: DiffEntry[] = [];
    const n = Math.max(a.length, b.length);
    for (let i = 0; i < n; i++) {
      if (i >= a.length) out.push({ path: join(path, i), kind: 'added', after: b[i] });
      else if (i >= b.length) out.push({ path: join(path, i), kind: 'removed', before: a[i] });
      else out.push(...diffJson(a[i], b[i], join(path, i)));
    }
    return out;
  }
  if (isRecord(a) && isRecord(b)) {
    const out: DiffEntry[] = [];
    const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
    for (const k of keys) {
      if (!(k in a)) out.push({ path: join(path, k), kind: 'added', after: b[k] });
      else if (!(k in b)) out.push({ path: join(path, k), kind: 'removed', before: a[k] });
      else out.push(...diffJson(a[k], b[k], join(path, k)));
    }
    return out;
  }
  if (a === undefined) return [{ path: path || '(root)', kind: 'added', after: b }];
  if (b === undefined) return [{ path: path || '(root)', kind: 'removed', before: a }];
  return [{ path: path || '(root)', kind: 'changed', before: a, after: b }];
}

/** Keys the user may not edit in the review form: identity and pinning are set by the server/release. */
export const LOCKED_SPEC_KEYS = new Set(['schema_version', 'model_key', 'model_version', 'baseline_release_id', 'knowledge_cutoff']);

/** Set a value at a path of string/number keys, returning a structurally shared copy. */
export function setIn(obj: unknown, path: (string | number)[], value: unknown): unknown {
  if (path.length === 0) return value;
  const [head, ...rest] = path;
  if (Array.isArray(obj) && typeof head === 'number') {
    const copy = obj.slice();
    copy[head] = setIn(obj[head], rest, value);
    return copy;
  }
  const base = isRecord(obj) ? obj : {};
  return { ...base, [head as string]: setIn(base[head as string], rest, value) };
}

/** Pin a draft spec to the chosen release (id and cutoff come from the release, never from the LLM). */
export function pinToRelease(spec: Record<string, unknown>, release: { id: string; knowledge_cutoff?: string | null } | null): Record<string, unknown> {
  if (!release) return spec;
  const out: Record<string, unknown> = { ...spec, baseline_release_id: release.id };
  if (release.knowledge_cutoff) out.knowledge_cutoff = release.knowledge_cutoff;
  return out;
}
