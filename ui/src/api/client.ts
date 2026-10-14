/**
 * Typed REVA API client. Every function maps to one /v1 endpoint.
 * See http.ts for auth/CSRF/idempotency handling.
 */
import { request, type Query } from './http';
import type {
  Accepted,
  BeliefEstimate,
  BeliefRelease,
  Concept,
  ConnectorHealth,
  CoverageData,
  Entity,
  Envelope,
  EventRecord,
  EvidenceItem,
  Gap,
  Job,
  LocationsData,
  ModelEvaluation,
  ModelInfo,
  Observation,
  Proposal,
  ProvenanceGraph,
  Relation,
  ReviewDecision,
  ScenarioCreated,
  ScenarioSpec,
  SessionInfo,
  SimulationRecord,
  Source,
  Timeseries,
} from './types';

export { ApiError } from './problem';
export type { ProblemDetails } from './problem';
export { newIdempotencyKey, onUnauthorized, readCookie, CSRF_COOKIE, CSRF_HEADER, IDEMPOTENCY_HEADER } from './http';

const V1 = '/v1';

async function get<T>(path: string, query?: Query, signal?: AbortSignal): Promise<T> {
  return (await request<T>('GET', `${V1}${path}`, { query, signal })).body;
}

const enc = encodeURIComponent;

// ------------------------------------------------------------------ session

export async function login(username: string, password: string): Promise<SessionInfo> {
  return (await request<SessionInfo>('POST', `${V1}/session`, { body: { username, password } })).body;
}

export async function logout(): Promise<void> {
  await request<unknown>('DELETE', `${V1}/session`);
}

// ------------------------------------------------------------------ world register / evidence / belief

export const getEntities = (q?: Query, s?: AbortSignal) => get<Envelope<Entity[]>>('/entities', q, s);
export const getEntity = (id: string, s?: AbortSignal) => get<Envelope<Entity>>(`/entities/${enc(id)}`, undefined, s);
export const getRelations = (q?: Query, s?: AbortSignal) => get<Envelope<Relation[]>>('/relations', q, s);
export const getEvents = (q?: Query, s?: AbortSignal) => get<Envelope<EventRecord[]>>('/events', q, s);
export const getObservations = (q?: Query, s?: AbortSignal) => get<Envelope<Observation[]>>('/observations', q, s);
export const getTimeseries = (q?: Query, s?: AbortSignal) => get<Envelope<Timeseries[]>>('/timeseries', q, s);
export const getBeliefs = (q?: Query, s?: AbortSignal) => get<Envelope<BeliefEstimate[]>>('/beliefs', q, s);
export const getBeliefRelease = (id: string, s?: AbortSignal) =>
  get<Envelope<BeliefRelease>>(`/belief-releases/${enc(id)}`, undefined, s);
export const getEvidence = (q?: Query, s?: AbortSignal) => get<Envelope<EvidenceItem[]>>('/evidence', q, s);
export const getEvidenceItem = (id: string, s?: AbortSignal) =>
  get<Envelope<EvidenceItem>>(`/evidence/${enc(id)}`, undefined, s);
export const getProvenance = (kind: string, id: string, q?: Query, s?: AbortSignal) =>
  get<Envelope<ProvenanceGraph>>(`/provenance/${enc(kind)}/${enc(id)}`, q, s);
export const getLocations = (q?: Query, s?: AbortSignal) => get<Envelope<LocationsData>>('/locations', q, s);

// ------------------------------------------------------------------ models

export const getModels = (q?: Query, s?: AbortSignal) => get<Envelope<ModelInfo[]>>('/models', q, s);
export const getModelEvaluations = (id: string, q?: Query, s?: AbortSignal) =>
  get<Envelope<ModelEvaluation[]>>(`/models/${enc(id)}/evaluations`, q, s);

// ------------------------------------------------------------------ simulation (work-starting POSTs carry Idempotency-Key)

export async function createScenario(spec: ScenarioSpec, idempotencyKey: string | true = true) {
  return request<Envelope<ScenarioCreated> | ScenarioCreated>('POST', `${V1}/scenarios`, { body: spec, idempotencyKey });
}

export async function startSimulation(
  body: { scenario_id: string; fidelity: string },
  idempotencyKey: string | true = true,
) {
  return request<Accepted | Envelope<Accepted>>('POST', `${V1}/simulations`, { body, idempotencyKey });
}

export const getSimulation = (id: string, s?: AbortSignal) =>
  get<Envelope<SimulationRecord>>(`/simulations/${enc(id)}`, undefined, s);

export async function startReplay(body: Record<string, unknown>, idempotencyKey: string | true = true) {
  return request<Accepted | Envelope<Accepted>>('POST', `${V1}/replay`, { body, idempotencyKey });
}

// ------------------------------------------------------------------ sources / acquisition

export const getSources = (q?: Query, s?: AbortSignal) => get<Envelope<Source[]>>('/sources', q, s);
export const getConnectors = (q?: Query, s?: AbortSignal) => get<Envelope<ConnectorHealth[]>>('/connectors', q, s);
export const getOntology = (q?: Query, s?: AbortSignal) => get<Envelope<Concept[]>>('/ontology', q, s);
export const getCoverage = (q?: Query, s?: AbortSignal) => get<Envelope<CoverageData>>('/coverage', q, s);
export const getGaps = (q?: Query, s?: AbortSignal) => get<Envelope<Gap[]>>('/gaps', q, s);

export async function startDiscovery(body: Record<string, unknown>, idempotencyKey: string | true = true) {
  return request<Accepted | Envelope<Accepted>>('POST', `${V1}/discovery`, { body, idempotencyKey });
}

export const getProposals = (q?: Query, s?: AbortSignal) => get<Envelope<Proposal[]>>('/proposals', q, s);

export async function reviewProposal(
  id: string,
  decision: ReviewDecision,
  comment: string,
  idempotencyKey: string | true = true,
) {
  return request<Envelope<unknown> | unknown>('POST', `${V1}/proposals/${enc(id)}/review`, {
    body: { decision, comment },
    idempotencyKey,
  });
}

// ------------------------------------------------------------------ query / jobs

export async function runQuery(spec: Record<string, unknown>) {
  return request<Envelope<unknown>>('POST', `${V1}/query`, { body: spec });
}

export const getJob = (id: string, s?: AbortSignal) => get<Envelope<Job> | Job>(`/jobs/${enc(id)}`, undefined, s);

export async function cancelJob(id: string) {
  return request<Envelope<Job> | Job>('POST', `${V1}/jobs/${enc(id)}/cancel`, { idempotencyKey: true });
}

/** Unwrap a body that may or may not be an envelope. */
export function unwrap<T>(body: Envelope<T> | T): T {
  if (body && typeof body === 'object' && 'data' in (body as object)) return (body as Envelope<T>).data;
  return body as T;
}
