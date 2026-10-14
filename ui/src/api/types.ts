/**
 * Hand-written mirror of the REVA API response models (src/reva/api/schemas.py).
 * Keep aligned with `npm run gen:types` output (src/api/openapi.gen.ts) once
 * contracts/openapi.json is exported.
 */

// ---------------------------------------------------------------- kernel enums

export type ContextMode = 'evidence' | 'belief' | 'simulation' | 'evaluation' | 'population' | 'quarantine';
export type Origin = 'live' | 'test_fixture';
export type Modality =
  | 'reported_observation'
  | 'selected_estimate'
  | 'forecast'
  | 'identified_causal_estimate'
  | 'assumption_conditioned_scenario'
  | 'unsupported';

export type MissingReason =
  | 'not_collected'
  | 'unavailable'
  | 'not_applicable'
  | 'suppressed'
  | 'source_error'
  | 'unmatched_semantics'
  | 'outside_support'
  | 'not_extracted';

export type UncertaintyStatus = 'unquantified' | 'exact_by_definition' | 'quantified';
export type UncertaintyKind =
  | 'unknown'
  | 'definition'
  | 'distribution'
  | 'bounds'
  | 'confidence_interval'
  | 'credible_interval'
  | 'prediction_interval'
  | 'sensitivity_range'
  | 'probability';

// ---------------------------------------------------------------- values

export interface MeasurementBasis {
  currency?: string | null;
  price_basis?: 'nominal' | 'real' | 'ppp' | null;
  price_reference_year?: number | null;
  exchange_rate_transform?: string | null;
  valuation?: string | null;
  scale_factor?: string;
  notes?: string | null;
}

export interface ScalarDecimal {
  kind: 'decimal';
  value: string;
  unit_ref: string;
  basis?: MeasurementBasis | null;
}
export interface ScalarFloat {
  kind: 'float';
  value: number;
  unit_ref: string;
  basis?: MeasurementBasis | null;
}
export interface CategoryValue {
  kind: 'category';
  code: string;
  vocabulary_version: string;
}
export interface TextValue {
  kind: 'text';
  language_tag: string;
  text?: string | null;
  artifact_sha256?: string | null;
}
export interface EntityRefValue {
  kind: 'entity';
  entity_id: string;
}
export interface ArrayValue {
  kind: 'array';
  artifact_sha256: string;
  dtype: string;
  shape: number[];
  axes: string[];
  unit_ref: string;
}
export interface DistributionValue {
  kind: 'distribution';
  uncertainty_ref: string;
}
export interface GeometryValue {
  kind: 'geometry';
  spatial_support_ref: string;
}
export interface MissingValueT {
  kind: 'missing';
  reason: MissingReason | string;
}

export type Value =
  | ScalarDecimal
  | ScalarFloat
  | CategoryValue
  | TextValue
  | EntityRefValue
  | ArrayValue
  | DistributionValue
  | GeometryValue
  | MissingValueT;

// ---------------------------------------------------------------- uncertainty

export interface Uncertainty {
  status: UncertaintyStatus;
  kind: UncertaintyKind;
  level?: number | null;
  dependence?: string | null;
  description?: string | null;
  method_version?: string | null;
  lower?: string | null;
  upper?: string | null;
  conditioning?: string[] | null;
  dependence_group?: string | null;
}

// ---------------------------------------------------------------- envelope

export interface ReleaseRef {
  id: string;
  sequence: number | null;
  knowledge_cutoff: string | null;
  published_at: string | null;
  origin?: Origin | null;
}

export interface ValidTime {
  start?: string | null;
  end?: string | null;
  label?: string | null;
  precision?: string | null;
  [k: string]: unknown;
}

export interface ProvenanceLink {
  kind: string;
  id: string;
  href?: string | null;
  label?: string | null;
}

export interface Warning {
  code?: string;
  message?: string;
  [k: string]: unknown;
}

export interface Envelope<T> {
  data: T;
  context_id?: string | null;
  context_mode?: ContextMode | null;
  origin?: Origin | null;
  modality?: Modality | null;
  estimand?: string | null;
  units?: unknown;
  support?: unknown;
  baseline_release?: ReleaseRef | null;
  knowledge_cutoff?: string | null;
  valid_time?: ValidTime | string | null;
  model_versions?: unknown;
  assumptions?: unknown;
  uncertainty?: Uncertainty | null;
  data_freshness?: unknown;
  evidence_profile?: unknown;
  applicability?: unknown;
  numerical_error?: unknown;
  compression_error?: unknown;
  model_disagreement?: unknown;
  historical_validation?: unknown;
  provenance_links?: ProvenanceLink[] | null;
  warnings?: (Warning | string)[] | null;
  cost?: unknown;
  next_cursor?: string | null;
}

/** Context fields of an envelope (everything except data). */
export type EnvelopeContext = Omit<Envelope<unknown>, 'data'>;

// ---------------------------------------------------------------- session

export interface SessionInfo {
  username?: string;
  scopes?: string[];
  expires_at?: string;
  csrf_token?: string;
  [k: string]: unknown;
}

// ---------------------------------------------------------------- jobs

export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'cancel_requested' | 'dead' | string;

export interface Accepted {
  job_id: string;
  status_url?: string | null;
  state?: JobState;
  status?: JobState;
  simulation_id?: string | null;
  scenario_id?: string | null;
  [k: string]: unknown;
}

export interface Job {
  id: string;
  job_type?: string;
  state?: JobState;
  status?: JobState;
  progress?: number | { done?: number; total?: number; fraction?: number; message?: string } | null;
  attempts?: number;
  max_attempts?: number;
  error?: unknown;
  last_error?: unknown;
  checkpoint?: unknown;
  cancel_requested?: boolean;
  cancellable?: boolean;
  result?: unknown;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  [k: string]: unknown;
}

// ---------------------------------------------------------------- domain records

export interface Entity {
  id: string;
  key?: string | null;
  kind?: string;
  kind_ref?: string;
  label?: string | null;
  aliases?: { provider: string; key: string; note?: string | null }[];
  [k: string]: unknown;
}

export interface Participant {
  role: string;
  entity_id?: string | null;
  label?: string | null;
  key?: string | null;
  value?: unknown;
}

export interface Relation {
  id: string;
  predicate?: string;
  predicate_ref?: string;
  layer?: string | null;
  participants: Participant[];
  value?: Value | null;
  valid_time?: ValidTime | string | null;
  support_id?: string | null;
  context_id?: string | null;
  [k: string]: unknown;
}

export interface EventRecord {
  id: string;
  kind?: string;
  label?: string | null;
  occurred_at?: string | null;
  recorded_at?: string | null;
  summary?: string | null;
  [k: string]: unknown;
}

export interface Observation {
  id: string;
  source_key?: string;
  concept_id?: string;
  subject_id?: string | null;
  subject_label?: string | null;
  logical_key?: string | null;
  value: Value;
  valid_time?: ValidTime | string | null;
  revision_no?: number | null;
  supersedes_id?: string | null;
  source_flags?: Record<string, unknown> | null;
  source_published_at?: string | null;
  source_available_at?: string | null;
  retrieved_at?: string | null;
  recorded_at?: string | null;
  raw_selector?: string | null;
  raw_sha256?: string | null;
  uncertainty?: Uncertainty | null;
  [k: string]: unknown;
}

export interface TimeseriesPoint {
  period: string;
  valid_time?: ValidTime | string | null;
  value: Value;
  uncertainty?: Uncertainty | null;
  observation_id?: string | null;
  estimate_id?: string | null;
  [k: string]: unknown;
}

export interface Timeseries {
  subject_id?: string;
  concept_id: string;
  points: TimeseriesPoint[];
  [k: string]: unknown;
}

export interface BeliefEstimate {
  id: string;
  subject_id: string;
  subject_label?: string | null;
  concept_id: string;
  concept_version?: string;
  semantic_profile?: SemanticProfile | null;
  support_id?: string;
  temporal_extent?: ValidTime | null;
  modality?: Modality;
  value: Value;
  uncertainty?: Uncertainty | null;
  alternatives?: unknown;
  discrepancy_flags?: unknown;
  observation_id?: string | null;
  source_flags?: Record<string, unknown> | null;
  revision_no?: number | null;
  source_key?: string | null;
  retrieved_at?: string | null;
  [k: string]: unknown;
}

export interface BeliefRelease extends ReleaseRef {
  origin?: Origin | null;
  method?: string | null;
  manifest_sha256?: string | null;
  estimate_count?: number | null;
  [k: string]: unknown;
}

export interface SemanticProfile {
  population_universe?: string;
  temporal_support?: string;
  spatial_support?: string;
  aggregation?: string;
  scale_type?: string;
  unit_ref?: string;
  currency_basis?: string;
  price_basis?: string;
  valuation?: string;
  permitted_transformations?: string[];
  missingness?: string[];
  measurement_method?: string;
  [k: string]: unknown;
}

export interface Concept {
  concept_id: string;
  version: string;
  kind?: string;
  label?: string | null;
  labels?: Record<string, string>;
  definition?: string | null;
  semantic_profile?: SemanticProfile | null;
  [k: string]: unknown;
}

export interface EvidenceItem {
  id: string;
  kind?: string; // 'card' | 'observation' | 'study' | ...
  title?: string | null;
  claim_id?: string | null;
  source_key?: string | null;
  recorded_at?: string | null;
  [k: string]: unknown;
}

export interface ProvenanceNode {
  id: string;
  kind: string;
  label?: string | null;
  sha256?: string | null;
  media_type?: string | null;
  selector?: string | null;
  storage_ref?: string | null;
  retrieved_at?: string | null;
  url?: string | null;
  [k: string]: unknown;
}

export interface ProvenanceEdge {
  from: string;
  to: string;
  role?: string | null;
  selector?: string | null;
  [k: string]: unknown;
}

export interface ProvenanceGraph {
  root?: { kind: string; id: string };
  nodes: ProvenanceNode[];
  edges: ProvenanceEdge[];
  truncated?: boolean;
  [k: string]: unknown;
}

export interface GeoFeatureProps {
  feature_key?: string;
  entity_id?: string | null;
  entity_key?: string | null;
  match_status?: 'reviewed_match' | 'unmatched' | 'disputed' | string;
  display_only?: boolean;
  display_note?: string | null;
  NAME?: string;
  ADM0_A3?: string;
  [k: string]: unknown;
}

export interface GeoFeature {
  type: 'Feature';
  id?: string | number;
  geometry: { type: string; coordinates: unknown } | null;
  properties: GeoFeatureProps;
}

export interface LocationsData {
  type?: 'FeatureCollection';
  features: GeoFeature[];
  dataset_sha256?: string | null;
  authority_note?: string | null;
  [k: string]: unknown;
}

export interface ModelInfo {
  id: string;
  model_key?: string;
  version?: string;
  name?: string | null;
  kind?: string | null;
  applicability?: unknown;
  status?: string | null;
  [k: string]: unknown;
}

export interface ModelEvaluation {
  id: string;
  model_id?: string;
  replay_mode?: 'operational_as_known' | 'available_then_reconstruction' | 'revised_data_retrospective' | string;
  metrics?: Record<string, unknown>;
  scores?: unknown;
  tournament?: unknown;
  [k: string]: unknown;
}

export interface Source {
  id?: string;
  source_key: string;
  name?: string | null;
  licence?: unknown;
  last_retrieved_at?: string | null;
  freshness?: unknown;
  [k: string]: unknown;
}

export interface ConnectorHealth {
  connector?: string;
  name?: string;
  status?: string;
  health?: Record<string, unknown> | null;
  [k: string]: unknown;
}

export interface CoverageRow {
  requirement_id?: string;
  id?: string;
  task?: string;
  concept?: string;
  concept_id?: string;
  numerator?: number;
  denominator?: number;
  satisfied?: number;
  required?: number;
  [k: string]: unknown;
}

export interface CoverageData {
  catalogue_version?: string;
  as_of?: string;
  rows?: CoverageRow[];
  requirements?: CoverageRow[];
  [k: string]: unknown;
}

export interface Gap {
  id?: string;
  requirement_id?: string;
  kind?: string;
  description?: string;
  entity?: string | null;
  concept_id?: string | null;
  [k: string]: unknown;
}

export interface Proposal {
  id: string;
  kind?: string;
  status?: string;
  title?: string | null;
  summary?: string | null;
  semantic_diff?: unknown;
  evidence?: unknown;
  created_at?: string;
  [k: string]: unknown;
}

export type ReviewDecision = 'approve' | 'reject';

export interface SimulationRecord {
  id: string;
  scenario_id?: string;
  state?: JobState;
  status?: JobState;
  fidelity?: 'L0' | 'L1' | 'L2';
  job_id?: string | null;
  result?: unknown;
  result_manifest?: unknown;
  assumptions?: unknown;
  uncertainty?: unknown;
  cost?: unknown;
  [k: string]: unknown;
}

// ---------------------------------------------------------------- scenario spec (contracts/scenario.schema.json)

export type CountryCode = 'FRA' | 'DZA' | 'CHN' | 'USA';
export type Fidelity = 'L0' | 'L1' | 'L2';
export type ScenarioOutput =
  | 'import_value_change_usd'
  | 'export_value_change_usd'
  | 'net_external_cost_change_usd'
  | 'exposure_percent_baseline_gdp';

export interface ElasticityPoint {
  import_elasticity: number;
  export_elasticity: number;
  basis: 'scenario_assumption';
}

export interface ScenarioSpec {
  schema_version: '1.0.0';
  title: string;
  baseline_release_id: string;
  knowledge_cutoff: string;
  model_key: 'economic.crude_exposure';
  model_version: '1.0.0';
  countries: CountryCode[];
  reference_year: number;
  horizon: { periods: 1; unit: 'annual_accounting_period' };
  intervention: {
    kind: 'set_exogenous_border_price_multiplier';
    import_multiplier: number;
    export_multiplier: number;
    unit: 'ratio';
  };
  assumptions: {
    quantity_response: 'fixed' | 'elasticity_sensitivity';
    elasticity_grid: ElasticityPoint[];
    no_general_equilibrium: true;
  };
  fidelity: Fidelity;
  missing_input_policy: 'fail';
  outputs: ScenarioOutput[];
  seed: number;
}

export interface ScenarioCreated {
  id?: string;
  scenario_id?: string;
  digest?: string;
  [k: string]: unknown;
}
