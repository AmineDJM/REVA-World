/**
 * Compile-time alignment check: every response model generated from
 * contracts/openapi.json (npm run gen:types) must be assignable to the
 * hand-written type the UI consumes. `npm run typecheck` fails on drift.
 */
import type { components } from './openapi.gen';
import type * as T from './types';

type S = components['schemas'];
type Assert<X extends true> = X;
type Fits<Generated, Hand> = [Generated] extends [Hand] ? true : false;

export type ContractChecks = [
  Assert<Fits<S['SessionInfo'], T.SessionInfo>>,
  Assert<Fits<S['Job'], T.Job>>,
  Assert<Fits<S['JobAccepted'], T.JobAccepted>>,
  Assert<Fits<S['JobCancelResult'], T.JobCancelResult>>,
  Assert<Fits<S['EntitySummary'], T.EntitySummary>>,
  Assert<Fits<S['EntityDetail'], T.EntityDetail>>,
  Assert<Fits<S['RelationGraph'], T.RelationGraph>>,
  Assert<Fits<S['Event'], T.EventRecord>>,
  Assert<Fits<S['Observation'], T.Observation>>,
  Assert<Fits<S['Timeseries'], T.Timeseries>>,
  Assert<Fits<S['BeliefEstimate'], T.BeliefEstimate>>,
  Assert<Fits<S['BeliefRelease'], T.BeliefRelease>>,
  Assert<Fits<S['EvidenceItem'], T.EvidenceItem>>,
  Assert<Fits<S['EvidenceDetail'], T.EvidenceDetail>>,
  Assert<Fits<S['ProvenanceGraph'], T.ProvenanceGraph>>,
  Assert<Fits<S['FeatureCollection'], T.FeatureCollection>>,
  Assert<Fits<S['ModelVersionOut'], T.ModelVersionOut>>,
  Assert<Fits<S['ModelEvaluation'], T.ModelEvaluation>>,
  Assert<Fits<S['ScenarioCreated'], T.ScenarioCreated>>,
  Assert<Fits<S['SimulationStatus'], T.SimulationStatus>>,
  Assert<Fits<S['ConnectorOut'], T.ConnectorOut>>,
  Assert<Fits<S['SourceOut'], T.SourceOut>>,
  Assert<Fits<S['Ontology'], T.Ontology>>,
  Assert<Fits<S['Coverage'], T.Coverage>>,
  Assert<Fits<S['Gap'], T.Gap>>,
  Assert<Fits<S['Proposal'], T.Proposal>>,
  Assert<Fits<S['ReviewRecorded'], T.ReviewRecorded>>,
  Assert<Fits<S['BeliefListResponse'], T.Envelope<T.BeliefEstimate[]>>>,
  Assert<Fits<S['SimulationStatusResponse'], T.Envelope<T.SimulationStatus>>>,
  Assert<Fits<S['CoverageResponse'], T.Envelope<T.Coverage>>>,
  // Request bodies the UI sends must be accepted by the API.
  Assert<Fits<T.SimulationRequest, S['SimulationRequest']>>,
  Assert<Fits<T.ReplayRequest, S['ReplayRequest']>>,
  Assert<Fits<T.DiscoveryRequest, S['DiscoveryRequest']>>,
  Assert<Fits<T.ReviewRequest, S['ReviewRequest']>>,
  Assert<Fits<T.LoginRequest, S['LoginRequest']>>,
];
