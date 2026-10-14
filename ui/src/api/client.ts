/**
 * Typed REVA API client. Every function maps to one /v1 endpoint
 * (src/reva/api/routes/*). See http.ts for auth/CSRF/idempotency handling.
 */
import { request, type Query } from './http';
import type {
  BeliefEstimate,
  BeliefRelease,
  ConnectorOut,
  Coverage,
  DiscoveryRequest,
  EntityDetail,
  EntitySummary,
  Envelope,
  EventRecord,
  EvidenceDetail,
  EvidenceItem,
  FeatureCollection,
  Gap,
  Job,
  JobAccepted,
  JobCancelResult,
  ModelEvaluation,
  ModelVersionOut,
  Observation,
  Ontology,
  Proposal,
  ProvenanceGraph,
  ProvenanceKind,
  QuerySpec,
  RelationGraph,
  ReplayRequest,
  ReviewDecision,
  ReviewRecorded,
  ScenarioCreated,
  ScenarioSpec,
  SessionInfo,
  SimulationRequest,
  SimulationStatus,
  SourceOut,
  Timeseries,
} from './types';

export { ApiError } from './problem';
export type { ProblemDetails } from './problem';
export { newIdempotencyKey, onUnauthorized, readCookie, CSRF_COOKIE, CSRF_HEADER, IDEMPOTENCY_HEADER } from './http';

const V1 = '/v1';
const enc = encodeURIComponent;

async function get<T>(path: string, query?: Query, signal?: AbortSignal): Promise<T> {
  return (await request<T>('GET', `${V1}${path}`, { query, signal })).body;
}

// ------------------------------------------------------------------ session

export async function login(username: string, password: string): Promise<SessionInfo> {
  return (await request<SessionInfo>('POST', `${V1}/session`, { body: { username, password } })).body;
}

export async function logout(): Promise<void> {
  await request<unknown>('DELETE', `${V1}/session`);
}

export const getSession = (s?: AbortSignal) => get<SessionInfo>('/session', undefined, s);

// ------------------------------------------------------------------ world register / evidence / belief

export const getEntities = (q?: Query, s?: AbortSignal) => get<Envelope<EntitySummary[]>>('/entities', q, s);
export const getEntity = (id: string, s?: AbortSignal) =>
  get<Envelope<EntityDetail>>(`/entities/${enc(id)}`, undefined, s);
export const getRelations = (q?: Query, s?: AbortSignal) => get<Envelope<RelationGraph>>('/relations', q, s);
export const getEvents = (q?: Query, s?: AbortSignal) => get<Envelope<EventRecord[]>>('/events', q, s);
export const getObservations = (q?: Query, s?: AbortSignal) => get<Envelope<Observation[]>>('/observations', q, s);
export const getTimeseries = (q?: Query, s?: AbortSignal) => get<Envelope<Timeseries>>('/timeseries', q, s);
export const getBeliefs = (q?: Query, s?: AbortSignal) => get<Envelope<BeliefEstimate[]>>('/beliefs', q, s);
export const getBeliefReleases = (q?: Query, s?: AbortSignal) =>
  get<Envelope<BeliefRelease[]>>('/belief-releases', q, s);
export const getBeliefRelease = (id: string, s?: AbortSignal) =>
  get<Envelope<BeliefRelease>>(`/belief-releases/${enc(id)}`, undefined, s);
export const getEvidence = (q?: Query, s?: AbortSignal) => get<Envelope<EvidenceItem[]>>('/evidence', q, s);
export const getEvidenceItem = (id: string, s?: AbortSignal) =>
  get<Envelope<EvidenceDetail>>(`/evidence/${enc(id)}`, undefined, s);
export const getProvenance = (kind: ProvenanceKind | string, id: string, q?: Query, s?: AbortSignal) =>
  get<Envelope<ProvenanceGraph>>(`/provenance/${enc(kind)}/${enc(id)}`, q, s);
export const getLocations = (q?: Query, s?: AbortSignal) => get<Envelope<FeatureCollection>>('/locations', q, s);

// ------------------------------------------------------------------ models

export const getModels = (q?: Query, s?: AbortSignal) => get<Envelope<ModelVersionOut[]>>('/models', q, s);
export const getModelEvaluations = (id: string, q?: Query, s?: AbortSignal) =>
  get<Envelope<ModelEvaluation[]>>(`/models/${enc(id)}/evaluations`, q, s);

// ------------------------------------------------------------------ simulation (work-starting POSTs carry Idempotency-Key)

export async function createScenario(spec: ScenarioSpec, idempotencyKey: string | true = true) {
  return (await request<ScenarioCreated>('POST', `${V1}/scenarios`, { body: spec, idempotencyKey })).body;
}

export async function startSimulation(body: SimulationRequest, idempotencyKey: string | true = true) {
  return (await request<JobAccepted>('POST', `${V1}/simulations`, { body, idempotencyKey })).body;
}

export const getSimulation = (id: string, s?: AbortSignal) =>
  get<Envelope<SimulationStatus>>(`/simulations/${enc(id)}`, undefined, s);

export async function startReplay(body: ReplayRequest, idempotencyKey: string | true = true) {
  return (await request<JobAccepted>('POST', `${V1}/replay`, { body, idempotencyKey })).body;
}

// ------------------------------------------------------------------ sources / acquisition

export const getSources = (q?: Query, s?: AbortSignal) => get<Envelope<SourceOut[]>>('/sources', q, s);
export const getConnectors = (q?: Query, s?: AbortSignal) => get<Envelope<ConnectorOut[]>>('/connectors', q, s);
export const getOntology = (q?: Query, s?: AbortSignal) => get<Envelope<Ontology>>('/ontology', q, s);
export const getCoverage = (q?: Query, s?: AbortSignal) => get<Envelope<Coverage>>('/coverage', q, s);
export const getGaps = (q?: Query, s?: AbortSignal) => get<Envelope<Gap[]>>('/gaps', q, s);

export async function startDiscovery(body: DiscoveryRequest, idempotencyKey: string | true = true) {
  return (await request<JobAccepted>('POST', `${V1}/discovery`, { body, idempotencyKey })).body;
}

export const getProposals = (q?: Query, s?: AbortSignal) => get<Envelope<Proposal[]>>('/proposals', q, s);

export async function reviewProposal(
  id: string,
  decision: ReviewDecision,
  comment: string,
  idempotencyKey: string | true = true,
) {
  return (
    await request<ReviewRecorded>('POST', `${V1}/proposals/${enc(id)}/review`, {
      body: { decision, comment },
      idempotencyKey,
    })
  ).body;
}

// ------------------------------------------------------------------ query / jobs

export async function runQuery(spec: QuerySpec) {
  return (await request<Envelope<unknown>>('POST', `${V1}/query`, { body: spec })).body;
}

export const getJob = (id: string, s?: AbortSignal) => get<Envelope<Job>>(`/jobs/${enc(id)}`, undefined, s);

export async function cancelJob(id: string) {
  return (await request<JobCancelResult>('POST', `${V1}/jobs/${enc(id)}/cancel`, {})).body;
}
