import type { EnvelopeMeta } from '../api/types';
import { contextModeLabel, formatDateTime, modalityLabel, shortId, validTimeLabel } from '../lib/format';

export type BannerMeta = Partial<EnvelopeMeta> | null | undefined;

export interface ContextBannerProps {
  meta: BannerMeta;
  /** Force the simulation treatment (e.g. the whole Simulation screen). */
  simulation?: boolean;
  /** Valid/reference time override when the screen filters by period. */
  referenceTime?: string;
  /** Label for what this context describes, e.g. "Country profile". */
  scope?: string;
}

export function isSimulationContext(meta: BannerMeta, forced = false): boolean {
  return forced || meta?.context_mode === 'simulation' || meta?.modality === 'assumption_conditioned_scenario';
}

export function isFixtureOrigin(meta: BannerMeta): boolean {
  return meta?.origin === 'test_fixture' || meta?.origin === 'mixed' || meta?.baseline_release?.origin === 'test_fixture';
}

/** The simulation banner text, reused in exports. */
export const SIMULATION_BANNER_TEXT =
  'SIMULATION — assumption-conditioned scenario. Values are hypothetical; they are not observations or forecasts.';

const CUTOFF_BASIS_LABEL: Record<string, string> = {
  release_cutoff: 'release cutoff',
  as_of: 'as of',
  scenario_cutoff: 'scenario cutoff',
  evaluation_context: 'evaluation cutoff',
  read_time: 'read as of',
};

/**
 * Shown on every screen: modality + context + valid/reference time + knowledge
 * cutoff. Simulation contexts get a persistent red banner; fixture-origin data
 * gets a visible TEST FIXTURE DATA label.
 */
export function ContextBanner({ meta, simulation = false, referenceTime, scope }: ContextBannerProps) {
  const sim = isSimulationContext(meta, simulation);
  const fixture = isFixtureOrigin(meta);
  const release = meta?.baseline_release;
  const cutoff = meta?.knowledge_cutoff ?? release?.knowledge_cutoff ?? null;
  const vt = referenceTime ?? (meta?.valid_time ? validTimeLabel(meta.valid_time) : 'not stated');

  return (
    <div className={`context-banner${sim ? ' context-simulation' : ''}`} role="region" aria-label="Result context">
      {sim && (
        <p className="simulation-flag" data-testid="simulation-banner">
          <strong>SIMULATION</strong> — assumption-conditioned scenario. Values are hypothetical; they are not
          observations or forecasts.
        </p>
      )}
      {fixture && (
        <p className="fixture-flag" data-testid="fixture-label">
          <strong>TEST FIXTURE DATA</strong>
          {meta?.origin === 'mixed' ? ' — some records come from captured test fixtures' : ' — captured fixture records, not a live acquisition'}
        </p>
      )}
      <dl className="context-fields">
        {scope && (
          <div>
            <dt>View</dt>
            <dd>{scope}</dd>
          </div>
        )}
        <div>
          <dt>Modality</dt>
          <dd>{meta ? modalityLabel(meta.modality) : 'not loaded'}</dd>
        </div>
        <div>
          <dt>Context</dt>
          <dd>
            {meta ? contextModeLabel(meta.context_mode) : 'not loaded'}
            {meta?.context_id && (
              <span className="mono" title={meta.context_id}>
                {' '}
                {shortId(meta.context_id)}
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt>Valid / reference time</dt>
          <dd>{vt}</dd>
        </div>
        <div>
          <dt>Knowledge cutoff</dt>
          <dd>
            {cutoff ? formatDateTime(cutoff) : 'not stated'}
            {cutoff && meta?.knowledge_cutoff && meta.knowledge_cutoff_basis && (
              <span className="muted"> ({CUTOFF_BASIS_LABEL[meta.knowledge_cutoff_basis] ?? meta.knowledge_cutoff_basis})</span>
            )}
          </dd>
        </div>
        {release && (
          <div>
            <dt>Baseline release</dt>
            <dd>
              <span className="mono" title={release.id}>
                {shortId(release.id)}
              </span>
              {release.sequence !== null && release.sequence !== undefined && ` · #${release.sequence}`}
              {release.published_at && ` · published ${formatDateTime(release.published_at)}`}
            </dd>
          </div>
        )}
        {meta?.origin && (
          <div>
            <dt>Origin</dt>
            <dd>{meta.origin === 'none' ? 'no records' : meta.origin.replace('_', ' ')}</dd>
          </div>
        )}
      </dl>
      {meta?.warnings && meta.warnings.length > 0 && (
        <ul className="context-warnings" aria-label="Warnings">
          {meta.warnings.map((w, i) => (
            <li key={`${w.code}-${i}`}>
              <strong>{w.code.replace(/_/g, ' ')}:</strong> {w.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
