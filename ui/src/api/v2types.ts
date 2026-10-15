/**
 * V2 contract mirror (docs/roadmap/v2_design.md). These shapes are written against the
 * design contracts and reconciled with contracts/openapi.json / src/reva/api/routes/*
 * as the backend lands. Fields the backend may not send yet are optional, and every
 * consumer goes through a normaliser in src/lib/* that tolerates their absence:
 * nothing missing is ever replaced by a made-up value.
 */
import type { SimResult } from './types';

// ---------------------------------------------------------------- natural-language drafts (NLC)

/** POST /v1/scenario-drafts (scope simulate). Nothing runs: the response is a draft only. */
export interface ScenarioDraftRequest {
  text: string;
  /** Pin the draft to this published belief release (the server fills cutoff from it). */
  baseline_release_id?: string;
}

/** One part of the request that the draft does not express (typed capability gap / refusal). */
export interface DraftUnsupportedItem {
  text?: string | null;
  capability_key?: string | null;
  /** capability_gap | policy_refusal */
  kind?: string | null;
  reason?: string | null;
  /** llm | server_guard | server | validator | provider */
  source?: string | null;
  /** The LLM proposed a capability key outside the allow-list (replaced by the server). */
  llm_capability_key_rejected?: boolean | null;
}

/** A number the draft proposes, with where it came from. */
export interface DraftNumber {
  path: string;
  value: unknown;
  /** stated | converted_from_stated | not_in_text | server_default | schema_constant */
  provenance?: string | null;
  quote?: string | null;
}

export interface DraftAssumption {
  text: string;
  source?: string | null;
}

export type DraftStatus = 'draft_ready' | 'needs_input' | 'draft_invalid' | 'draft_rejected' | 'nothing_draftable' | string;

/** Normalised view of the POST /v1/scenario-drafts response (src/reva/api/routes/nl.py ScenarioDraftResponse). */
export interface ScenarioDraft {
  status: DraftStatus;
  confirmation_required: boolean;
  /** Always false: drafting never executes anything. */
  executed: boolean;
  /** Valid ScenarioSpec (status draft_ready only). */
  draft_spec: Record<string, unknown> | null;
  draft_spec_sha256: string | null;
  /** Incomplete or invalid spec, for review only. */
  candidate_spec: Record<string, unknown> | null;
  validation: Record<string, unknown>;
  /** Server-generated restatement of the draft, in the input language. */
  restatement: string;
  language: string | null;
  /** The LLM's own paraphrase (untrusted, shown for comparison). */
  llm_restatement: string | null;
  unsupported: DraftUnsupportedItem[];
  rejected_ids: Record<string, unknown>[];
  missing_fields: string[];
  assumptions_added: DraftAssumption[];
  numbers_to_confirm: DraftNumber[];
  provider: string | null;
  model: string | null;
  model_requested: string | null;
  prompt_version: string | null;
  prompt_sha256: string | null;
  catalog_digest: string | null;
  usage: Record<string, unknown> | null;
  latency_ms: number | null;
  fallback_from: Record<string, unknown>[];
  warnings: string[];
  next_step: string | null;
}

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
