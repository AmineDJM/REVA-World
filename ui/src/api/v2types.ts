/**
 * V2 contract mirror (docs/roadmap/v2_design.md). These shapes are written against the
 * design contracts and reconciled with contracts/openapi.json / src/reva/api/routes/*
 * as the backend lands. Fields the backend may not send yet are optional, and every
 * consumer goes through a normaliser in src/lib/* that tolerates their absence:
 * nothing missing is ever replaced by a made-up value.
 */
import type { EnvelopeMeta, SimResult } from './types';

// ---------------------------------------------------------------- natural-language drafts (NLC)

/** POST /v1/scenario-drafts (scope simulate). Nothing runs: the response is a draft only. */
export interface ScenarioDraftRequest {
  text: string;
  /** Pin the draft to this published belief release (the server fills cutoff from it). */
  baseline_release_id?: string;
}

/** One part of the request the drafting LLM (or the server's guard) could not express. */
export interface DraftUnsupportedItem {
  text?: string | null;
  capability_key?: string | null;
  /** capability_gap | policy_refusal (catalogue kinds). */
  kind?: string | null;
  reason?: string | null;
  /** Who flagged it: the LLM, or the server-side guard re-checking the draft. */
  source?: string | null;
}

export interface DraftParameter {
  path: string;
  number?: number | null;
  text?: string | null;
  /** The words of the user's text this value was taken from. */
  quote?: string | null;
}

export interface LlmUsageOut {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
}

export interface ScenarioDraft {
  draft_id?: string | null;
  /** The draft ScenarioSpec (null when nothing in the text is draftable). */
  spec: Record<string, unknown> | null;
  /** Server-side validation of `spec` by the same schema/compiler as hand-written specs. */
  valid?: boolean | null;
  validation_problems?: unknown[];
  restatement: string;
  title?: string | null;
  input_language?: string | null;
  unsupported: DraftUnsupportedItem[];
  assumptions_added: string[];
  parameters: DraftParameter[];
  provider?: string | null;
  model?: string | null;
  model_requested?: string | null;
  prompt_version?: string | null;
  prompt_sha256?: string | null;
  usage?: LlmUsageOut | null;
  /** True when replayed from a recorded response (tests/CI): never a live LLM call. */
  recorded?: boolean | null;
  latency_ms?: number | null;
  text_sha256?: string | null;
}

export type ScenarioDraftEnvelope = Partial<EnvelopeMeta> & { data: ScenarioDraft };

// ---------------------------------------------------------------- multi-period results (ENG)

/** A result row that may carry a half-open period [period_start, period_end) (UTC dates). */
export interface PeriodResult extends SimResult {
  period_start?: string | null;
  period_end?: string | null;
}

/** Known V2 model keys. Any other key renders through the generic multi-period table. */
export const MODEL_KEYS = {
  crude: 'economic.crude_exposure',
  cohort: 'demography.cohort_component',
  io: 'economic.io_price',
  retirement: 'social.retirement_age',
} as const;
