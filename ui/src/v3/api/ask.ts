/**
 * Questions client (agent NLQA, src/reva/api/routes/ask.py): POST /v1/ask.
 * The answer is composed only from cited evidence; every sentence lists its evidence ids.
 */
import { z } from 'zod';
import { JsonRecord, v3post } from './common';

export const AskSentence = z.looseObject({
  id: z.string(),
  text: z.string(),
  kind: z.string().default('fact'),
  citations: z.array(z.string()).default([]),
  citation_refs: z.array(z.string()).default([]),
});
export type AskSentence = z.output<typeof AskSentence>;

export const AskEvidence = z.looseObject({
  id: z.string(),
  kind: z.string(),
  citation: JsonRecord.nullish().transform((v) => v ?? {}),
});
export type AskEvidence = z.output<typeof AskEvidence>;

export const AskPlan = z.looseObject({
  planner: z.string().nullish(),
  language: z.string().nullish(),
  operations: z.array(JsonRecord).default([]),
  rejected: z.array(JsonRecord).default([]),
});

export const AskResponse = z.looseObject({
  question_sha256: z.string().nullish(),
  language: z.string().nullish(),
  plan: AskPlan,
  restatement: z.string().nullish(),
  llm_restatement: z.string().nullish(),
  answer_text: z.string(),
  sentences: z.array(AskSentence),
  evidence: z.array(AskEvidence).default([]),
  unsupported: z.array(JsonRecord).default([]),
  planner: z.string().nullish(),
  provider: z.string().nullish(),
  model: z.string().nullish(),
  prompt_version: z.string().nullish(),
  warnings: z.array(z.string()).default([]),
  latency_ms: z.number().nullish(),
});
export type AskResponse = z.output<typeof AskResponse>;

export const postAsk = (question: string, lang: 'fr' | 'en' = 'fr', s?: AbortSignal) => v3post('/v1/ask', AskResponse, { question, lang }, s);

/** Sentences that state an absence or a refusal (rendered differently, never as a value). */
export const NON_FACT_KINDS = new Set(['missing', 'unavailable', 'unsupported']);
