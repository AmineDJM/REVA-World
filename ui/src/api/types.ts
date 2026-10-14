/**
 * Hand-written mirror of the REVA API contract (src/reva/api/schemas.py).
 * Keep aligned with `npm run gen:types` (src/api/openapi.gen.ts, generated from
 * contracts/openapi.json). Field names here must match the Pydantic models.
 */

// ---------------------------------------------------------------- enums

export type ContextMode =
  | 'evidence'
  | 'belief'
  | 'simulation'
  | 'evaluation'
  | 'population'
  | 'registry'
  | 'operations';
export type RecordOrigin = 'live' | 'test_fixture';
export type OriginLabel = RecordOrigin | 'mixed' | 'none';
export type Modality =
  | 'reported_observation'
  | 'selected_estimate'
  | 'forecast'
  | 'identified_causal_estimate'
  | 'assumption_conditioned_scenario'
  | 'unsupported'
  | 'metadata'
  | 'evaluation';

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

// ---------------------------------------------------------------- typed values

export interface MeasurementBasis {
  currency?: string | null;
  price_basis?: string | null;
  price_reference_year?: number | null;
  exchange_rate_transform?: string | null;
  valuation?: string | null;
  scale_factor?: string;
  notes?: string | null;
  [k: string]: unknown;
}

export interface DecimalValue {
  kind: 'decimal';
  value: string;
  unit_ref: string;
  basis?: MeasurementBasis;
}
export interface FloatValue {
  kind: 'float';
  value: number;
  unit_ref: string;
  basis?: MeasurementBasis;
}
export interface MissingValueT {
  kind: 'missing';
  reason: MissingReason | string;
  detail?: string | null;
}
export interface OtherValue {
  kind: 'category' | 'text' | 'entity' | 'array' | 'distribution' | 'geometry';
  [k: string]: unknown;
}
export type TypedValue = DecimalValue | FloatValue | MissingValueT | OtherValue;

/** Full uncertainty record (reva.kernel.uncertainty.UncertaintySpec). */
export interface Uncertainty {
  status: UncertaintyStatus;
  kind: UncertaintyKind | string;
  description?: string | null;
  level?: number | null;
  lower?: string | null;
  upper?: string | null;
  dependence_group?: string | null;
  method_version?: string | null;
  conditioning?: string[];
}

/** Envelope-level uncertainty summary. */
export interface UncertaintySummary {
  status: UncertaintyStatus | 'mixed' | 'not_applicable';
  kind?: string | null;
  level?: number | null;
  dependence?: string | null;
  note?: string | null;
}

export interface Assessment {
  status: 'not_applicable' | 'not_assessed' | 'reported';
  reason?: string | null;
  detail?: Record<string, unknown> | null;
}

// ---------------------------------------------------------------- envelope

export interface BaselineReleaseRef {
  id: string;
  sequence: number | null;
  knowledge_cutoff: string;
  published_at: string | null;
  origin: RecordOrigin;
  method?: string | null;
}

export interface ModelVersionRef {
  id?: string | null;
  model_key: string;
  version: string;
  approval_status?: string | null;
}

export type ProvenanceKind = 'estimate' | 'observation' | 'acquisition' | 'artifact' | 'sim_result' | 'release';

export interface ProvenanceLink {
  kind: ProvenanceKind | 'run' | 'scenario';
  id: string;
  href: string;
}

export interface ApiWarning {
  code: string;
  message: string;
}

export interface CostReport {
  wall_time_ms: number;
  rows: number;
  detail?: Record<string, unknown> | null;
}

export interface Freshness {
  latest_retrieved_at?: string | null;
  latest_recorded_at?: string | null;
  note?: string | null;
}

export interface EnvelopeMeta {
  context_id?: string | null;
  context_mode: ContextMode;
  origin: OriginLabel;
  modality: Modality;
  estimand?: Record<string, unknown> | null;
  units: string[];
  support?: Record<string, unknown> | null;
  baseline_release?: BaselineReleaseRef | null;
  knowledge_cutoff?: string | null;
  valid_time?: Record<string, unknown> | null;
  model_versions: ModelVersionRef[];
  assumptions: string[];
  uncertainty: UncertaintySummary;
  data_freshness?: Freshness | null;
  evidence_profile: Assessment;
  applicability: Assessment;
  numerical_error: Assessment;
  compression_error: Assessment;
  model_disagreement: Assessment;
  historical_validation: Assessment;
  provenance_links: ProvenanceLink[];
  warnings: ApiWarning[];
  cost?: CostReport | null;
  next_cursor?: string | null;
}

export interface Envelope<T> extends EnvelopeMeta {
  data: T;
}

// ---------------------------------------------------------------- session / jobs

export interface LoginRequest {
  username: string;
  password: string;
}

export interface SessionInfo {
  username: string;
  principal: string;
  auth_method: 'session' | 'token';
  scopes: string[];
  expires_at?: string | null;
  csrf_token?: string | null;
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancel_requested' | 'cancelled' | 'dead';
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = ['succeeded', 'failed', 'cancelled', 'dead'];

export interface JobAccepted {
  job_id: string;
  job_type: string;
  status: string;
  status_url: string;
}

export interface Job {
  id: string;
  job_type: string;
  status: JobStatus;
  payload: Record<string, unknown>;
  priority: number;
  attempts: number;
  max_attempts: number;
  progress: Record<string, unknown>;
  has_checkpoint: boolean;
  result_ref?: Record<string, unknown> | null;
  last_error?: Record<string, unknown> | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  available_at: string;
  finished_at?: string | null;
  cancellable: boolean;
  links: Record<string, string>;
}

export interface JobCancelResult {
  job_id: string;
  status: string;
}

// ---------------------------------------------------------------- entities

export interface EntityRef {
  id: string;
  label: string;
  kind?: string | null;
}

export interface Alias {
  provider: string;
  external_key: string;
  confidence_kind: string;
  mapping_version: string;
  valid_range?: string | null;
}

export interface EntitySummary {
  id: string;
  label: string;
  kind: string;
  namespace: string;
  referent_mode: 'real' | 'abstract' | 'synthetic' | 'hypothetical';
  alias_count: number;
}

export interface EntityComponent {
  component: 'observations' | 'estimates' | 'display_geometry' | 'relations';
  concept_id?: string | null;
  layer?: string | null;
  count: number;
  first_period?: string | null;
  last_period?: string | null;
}

export interface SupportedContext {
  mode: 'evidence' | 'belief' | 'simulation';
  context_id?: string | null;
  release_id?: string | null;
  origin?: string | null;
  knowledge_cutoff?: string | null;
  description: string;
}

export interface EntityDetail {
  id: string;
  label: string;
  kind: string;
  namespace: string;
  referent_mode: string;
  recorded_at: string;
  aliases: Alias[];
  components: EntityComponent[];
  supported_contexts: SupportedContext[];
}

// ---------------------------------------------------------------- supports, relations, events

export interface TimeExtent {
  kind?: string;
  precision?: string;
  start?: string | null;
  end?: string | null;
  label?: string | null;
  [k: string]: unknown;
}

export interface SupportSummary {
  id: string;
  temporal: TimeExtent;
  spatial: Record<string, unknown>;
  population: Record<string, unknown>;
  aggregation: string;
  period_label?: string | null;
}

export interface Participant {
  ordinal: number;
  role: string;
  entity_id: string;
  entity_label: string;
  entity_kind: string;
  direction: 'in' | 'out' | 'undirected';
}

export interface Relation {
  id: string;
  predicate: string;
  layer: string;
  support: SupportSummary;
  component: Record<string, unknown>;
  existence_uncertainty: Record<string, unknown>;
  strength?: Record<string, unknown> | null;
  value?: TypedValue | null;
  concept_id?: string | null;
  evidence_observation_id?: string | null;
  release_id?: string | null;
  origin?: RecordOrigin | null;
  recorded_at: string;
  participants: Participant[];
}

export interface GraphNode {
  id: string;
  label: string;
  kind: string;
}

export interface RelationGraph {
  relations: Relation[];
  nodes: GraphNode[];
  truncated: boolean;
  max_edges: number;
  depth: number;
  matched_lower_bound: number;
}

export interface EventRecord {
  entity_id: string;
  label: string;
  event_type: string;
  support: SupportSummary;
  component: Record<string, unknown>;
}

// ---------------------------------------------------------------- observations / timeseries

export interface Observation {
  id: string;
  source_key: string;
  connector: string;
  connector_version: string;
  acquisition_id: string;
  origin: RecordOrigin;
  logical_key: string;
  revision_no: number;
  supersedes_id?: string | null;
  superseded_by_id?: string | null;
  source_revision_id?: string | null;
  subject: EntityRef;
  concept_id: string;
  concept_version: string;
  value: TypedValue;
  uncertainty: Uncertainty;
  source_flags: Record<string, unknown>;
  support: SupportSummary;
  observed_at?: string | null;
  source_published_at?: string | null;
  source_available_at?: string | null;
  availability_basis?: Record<string, unknown> | null;
  retrieved_at: string;
  recorded_at: string;
  raw_selector: Record<string, unknown>;
  parser_version: string;
  retracted: boolean;
}

export interface ConceptInfo {
  concept_id: string;
  version: string;
  kind: string;
  definition: string;
  unit_ref?: string | null;
  aggregation?: string | null;
  semantic_profile: SemanticProfile;
}

export interface RevisionEntry {
  observation_id: string;
  revision_no: number;
  value: TypedValue;
  source_revision_id?: string | null;
  retrieved_at: string;
  recorded_at: string;
  source_published_at?: string | null;
}

export interface TimeseriesPoint {
  period_label: string | null;
  valid_start: string | null;
  valid_end: string | null;
  temporal_kind: string;
  aggregation: string;
  support_id: string;
  source_key: string;
  logical_key: string;
  observation_id: string;
  revision_no: number;
  value: TypedValue;
  uncertainty: Uncertainty;
  recorded_at: string;
  retrieved_at: string;
  source_published_at?: string | null;
  origin: RecordOrigin;
  revisions?: RevisionEntry[] | null;
}

export interface Timeseries {
  subject: EntityRef;
  concept: ConceptInfo;
  revisions_mode: 'latest' | 'all';
  as_of?: string | null;
  points: TimeseriesPoint[];
}

// ---------------------------------------------------------------- beliefs

export interface BeliefEstimate {
  id: string;
  release_id: string;
  subject: EntityRef;
  concept_id: string;
  concept_version: string;
  support: SupportSummary;
  modality: 'selected_report' | 'estimated_state' | 'forecast';
  value: TypedValue;
  uncertainty: Uncertainty;
  alternatives: Record<string, unknown>[];
  discrepancy_flags: string[];
  source_key?: string | null;
  observation_id?: string | null;
  observation_revision_no?: number | null;
  retrieved_at?: string | null;
  method: ModelVersionRef;
}

export interface ReleaseConceptCount {
  concept_id: string;
  estimates: number;
  missing: number;
}

export interface BeliefRelease {
  id: string;
  sequence: number | null;
  published_at: string | null;
  knowledge_cutoff: string;
  origin: RecordOrigin;
  method: string;
  context_id: string;
  manifest_sha256: string;
  ontology_manifest_sha256: string;
  model_manifest_sha256: string;
  recorded_at: string;
  estimate_count: number;
  concepts: ReleaseConceptCount[];
  artifacts: { sha256: string; role: string }[];
}

// ---------------------------------------------------------------- evidence

export type EvidenceKindLabel = 'scientific_card' | 'study' | 'source_disagreement';

export interface EvidenceItem {
  id: string;
  kind: EvidenceKindLabel;
  title: string;
  status?: string | null;
  evidence_kind?: string | null;
  summary?: string | null;
  recorded_at?: string | null;
}

export interface SourceDisagreement {
  estimate_id: string;
  release_id: string;
  subject: EntityRef;
  concept_id: string;
  support: SupportSummary;
  chosen: Record<string, unknown>;
  alternatives: Record<string, unknown>[];
  discrepancy_flags: string[];
  note: string;
}

export interface EvidenceDetail {
  id: string;
  kind: EvidenceKindLabel;
  title: string;
  status?: string | null;
  evidence_kind?: string | null;
  card?: Record<string, unknown> | null;
  claim_key?: string | null;
  claim_version?: number | null;
  study?: Record<string, unknown> | null;
  effects: Record<string, unknown>[];
  disagreement?: SourceDisagreement | null;
  recorded_at?: string | null;
}

// ---------------------------------------------------------------- provenance

export type ProvenanceNodeKind =
  | 'estimate'
  | 'release'
  | 'observation'
  | 'activity'
  | 'acquisition'
  | 'artifact'
  | 'storage_location'
  | 'connector_version'
  | 'source'
  | 'sim_result'
  | 'run'
  | 'scenario'
  | 'context'
  | 'model_version';

export interface ProvenanceNode {
  id: string;
  kind: ProvenanceNodeKind;
  label: string;
  attributes: Record<string, unknown>;
}

export interface ProvenanceEdge {
  source: string;
  target: string;
  relation: string;
  attributes: Record<string, unknown>;
}

export interface ProvenanceGraph {
  root: string;
  nodes: ProvenanceNode[];
  edges: ProvenanceEdge[];
  truncated: boolean;
  max_nodes: number;
}

// ---------------------------------------------------------------- locations

export type MatchStatus = 'reviewed_match' | 'unmatched' | 'disputed' | 'overseas_part';

export interface GeoFeatureProperties {
  feature_key: string;
  entity_id?: string | null;
  entity_label?: string | null;
  match_status: MatchStatus;
  name?: string | null;
  dataset_sha256: string;
  display_only: true;
}

export interface GeoFeature {
  type: 'Feature';
  id: string;
  geometry: { type: string; coordinates: unknown; [k: string]: unknown };
  properties: GeoFeatureProperties;
}

export interface FeatureCollection {
  type: 'FeatureCollection';
  features: GeoFeature[];
  display_note: string;
  truncated: boolean;
}

// ---------------------------------------------------------------- models

export interface ModelVersionOut {
  id: string;
  model_key: string;
  version: string;
  kind: string;
  approval_status: string;
  applicability: unknown;
  assumptions: unknown;
  input_ports: unknown;
  output_ports: unknown;
  training_cutoff?: string | null;
  spec_sha256: string;
  recorded_at: string;
  evaluation_count: number;
}

export type ReplayMode = 'operational_as_known' | 'available_then_reconstruction' | 'revised_data_retrospective';

export interface ModelEvaluation {
  id: string;
  model_version_id: string;
  context_id: string;
  replay_mode: ReplayMode | string;
  task_scope: Record<string, unknown>;
  metrics: Record<string, unknown>;
  evaluation_manifest_sha256: string;
  recorded_at: string;
  promotions: Record<string, unknown>[];
}

// ---------------------------------------------------------------- scenarios / simulations

export interface ScenarioCreated {
  scenario_id: string;
  context_id: string;
  spec_digest: string;
  spec_sha256: string;
  baseline_release_id: string;
  knowledge_cutoff: string | null;
  created_by: string;
  recorded_at: string;
}

export interface Scenario {
  id: string;
  context_id: string;
  origin: RecordOrigin;
  title: string;
  baseline_release: BaselineReleaseRef;
  knowledge_cutoff: string | null;
  spec: Record<string, unknown>;
  spec_digest: string;
  spec_sha256: string;
  created_by: string;
  recorded_at: string;
}

export interface SimulationRequest {
  scenario_id: string;
  fidelity: Fidelity;
}

export interface SimRun {
  id: string;
  fidelity: string;
  model_manifest_sha256: string;
  ontology_manifest_sha256: string;
  seed_plan: Record<string, unknown>;
  execution_profile: Record<string, unknown>;
  recorded_at: string;
}

export interface SimOutcome {
  status: 'succeeded' | 'failed' | 'unsupported' | 'missing_input';
  output_manifest_sha256?: string | null;
  output_digest?: string | null;
  applicability: Record<string, unknown>;
  problems: unknown[];
  cost: Record<string, unknown>;
  cache_hit_of?: string | null;
  recorded_at: string;
}

export interface SimResult {
  id: string;
  output_key: string;
  subject?: EntityRef | null;
  variant: string;
  value: TypedValue;
  uncertainty: Uncertainty;
  applicability: Record<string, unknown>;
  artifact_sha256?: string | null;
}

export interface SimulationStatus {
  job: Job | null;
  scenario: Scenario | null;
  run: SimRun | null;
  outcome: SimOutcome | null;
  results: SimResult[];
}

export interface ReplayRequest {
  mode: ReplayMode;
  cutoff: string;
  concepts?: string[];
  subjects?: string[];
  model_keys?: string[];
  horizon_years?: number;
}

export interface DiscoveryRequest {
  catalogue?: 'eurostat' | 'worldbank' | 'comtrade';
  query: string;
  concept_id?: string | null;
  max_candidates?: number;
}

// ---------------------------------------------------------------- sources / connectors / ontology

export interface ConnectorHealth {
  status: 'healthy' | 'degraded' | 'quarantined' | 'unknown';
  metrics: Record<string, unknown>;
  recorded_at: string;
}

export interface ConnectorOut {
  id: string;
  name: string;
  version: string;
  source_key: string;
  licence?: string | null;
  declared_capabilities: Record<string, unknown>;
  spec_sha256: string;
  code_digest: string;
  recorded_at: string;
  latest_health?: ConnectorHealth | null;
  cursor?: Record<string, unknown> | null;
  acquisitions: number;
  last_retrieved_at?: string | null;
}

export interface SourceOut {
  id: string;
  source_key: string;
  title: string;
  provider_url: string;
  lineage_family: string;
  rights: Record<string, unknown>;
  metadata: Record<string, unknown>;
  metadata_version?: number | null;
  connectors: string[];
  observations: number;
  last_retrieved_at?: string | null;
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

export interface ConceptOut {
  id: string;
  concept_id: string;
  version: string;
  kind: string;
  labels: Record<string, unknown>;
  definition: string;
  owner: string;
  semantic_profile: SemanticProfile;
  registry_sha256: string;
  recorded_at: string;
}

export interface MappingOut {
  id: string;
  mapping_key: string;
  version: string;
  source_ref: Record<string, unknown>;
  target_concept_id: string;
  target_concept_version: string;
  mapping_kind: string;
  transform_spec: Record<string, unknown>;
  applicability: Record<string, unknown>;
  approval: string;
  recorded_at: string;
}

export interface Ontology {
  concepts: ConceptOut[];
  mappings: MappingOut[];
}

// ---------------------------------------------------------------- coverage / gaps

export interface RequirementCoverage {
  requirement_id: string;
  task: string;
  concept: string;
  geography: string[];
  period: number[];
  weight: number;
  acceptable_freshness_days: number;
  denominator: number;
  available: number;
  typed_missing: number;
  absent: number;
  semantically_compatible: number;
  fresh: number;
  availability_ratio: string;
  semantic_ratio: string;
  freshness_ratio: string;
}

export interface CoverageTotals {
  denominator: number;
  available: number;
  semantically_compatible: number;
  fresh: number;
  weighted_denominator: number;
  weighted_available: number;
}

export interface Coverage {
  catalogue_version: string;
  statement: string;
  release_id: string | null;
  requirements: RequirementCoverage[];
  totals: CoverageTotals;
}

export type GapKind = 'typed_missing' | 'absent' | 'stale' | 'semantic_mismatch' | 'no_release';

export interface Gap {
  requirement_id: string;
  concept: string;
  entity_key: string;
  entity_id: string | null;
  period: number;
  gap_kind: GapKind;
  reason: string;
  source_flags?: Record<string, unknown> | null;
  action: string;
}

// ---------------------------------------------------------------- proposals

export interface ProposalReviewOut {
  id: string;
  decision: string;
  reviewer: string;
  comment: string;
  recorded_at: string;
}

export interface GateResultOut {
  id: string;
  gate_name: string;
  policy_version: string;
  decision: string;
  evidence_sha256: string;
  evaluator: string;
  recorded_at: string;
}

export interface Proposal {
  id: string;
  proposal_type: string;
  manifest_sha256: string;
  permission_tier: number;
  summary: Record<string, unknown>;
  author: string;
  recorded_at: string;
  reviews: ProposalReviewOut[];
  gate_results: GateResultOut[];
}

export type ReviewDecision = 'approve' | 'reject' | 'request_changes';

export interface ReviewRequest {
  decision: ReviewDecision;
  comment: string;
}

export interface ReviewRecorded {
  review_id: string;
  proposal_id: string;
  decision: string;
  reviewer: string;
}

// ---------------------------------------------------------------- query spec

export type QuerySpec =
  | { kind: 'timeseries'; subject_id: string; concept: string; as_of?: string | null; revisions?: 'latest' | 'all' }
  | {
      kind: 'beliefs';
      release_id?: string | null;
      as_of?: string | null;
      subject_ids?: string[];
      concepts?: string[];
      limit?: number;
    }
  | {
      kind: 'relations';
      entity_id: string;
      layer?: string | null;
      period?: number | null;
      as_of?: string | null;
      depth?: number;
      max_edges?: number;
    };

// ---------------------------------------------------------------- scenario spec (contracts/scenario.schema.json)

export type CountryCode = 'FRA' | 'DZA' | 'CHN' | 'USA';
export const COUNTRY_CODES: readonly CountryCode[] = ['FRA', 'DZA', 'CHN', 'USA'];
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
