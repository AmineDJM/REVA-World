import type { EnvelopeMeta, OriginLabel } from '../api/types';
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
  /**
   * Origin of the records actually shown on the page (observations / estimates). Used when the
   * envelope itself reports `origin: none` (e.g. the entity endpoint, which holds no records of
   * its own), so the banner never says "no records" next to fixture data.
   */
  dataOrigin?: OriginLabel | null;
}

/** Combine record origins: all live, all test_fixture, both (mixed) or none. */
export function combineOrigins(origins: Iterable<string | null | undefined>): OriginLabel {
  let live = false;
  let fixture = false;
  for (const o of origins) {
    if (o === 'live') live = true;
    else if (o === 'test_fixture') fixture = true;
    else if (o === 'mixed') live = fixture = true;
  }
  if (live && fixture) return 'mixed';
  if (fixture) return 'test_fixture';
  if (live) return 'live';
  return 'none';
}

/** The origin to display and whether it was derived from the page's data rather than the envelope. */
export function displayOrigin(meta: BannerMeta, dataOrigin?: OriginLabel | null): { origin: OriginLabel | null; derived: boolean } {
  const own = meta?.origin ?? null;
  if ((own === null || own === 'none') && dataOrigin && dataOrigin !== 'none') return { origin: dataOrigin, derived: true };
  return { origin: own, derived: false };
}

const ORIGIN_TEXT: Record<OriginLabel, string> = {
  live: 'live',
  test_fixture: 'test fixture',
  mixed: 'mixed (live and test fixture)',
  none: 'no records',
};

export function isSimulationContext(meta: BannerMeta, forced = false): boolean {
  return forced || meta?.context_mode === 'simulation' || meta?.modality === 'assumption_conditioned_scenario';
}

export function isFixtureOrigin(meta: BannerMeta): boolean {
  return meta?.origin === 'test_fixture' || meta?.origin === 'mixed' || meta?.baseline_release?.origin === 'test_fixture';
}

/** The simulation banner text, reused in exports. */
export const SIMULATION_BANNER_TEXT =
  'SIMULATION — assumption-conditioned stress test. Values are hypothetical consequences of the stated assumptions; they are not observations or forecasts and not an estimate of GDP loss.';

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
export function ContextBanner({ meta, simulation = false, referenceTime, scope, dataOrigin }: ContextBannerProps) {
  const sim = isSimulationContext(meta, simulation);
  const origin = displayOrigin(meta, dataOrigin);
  const fixture = isFixtureOrigin(meta) || origin.origin === 'test_fixture' || origin.origin === 'mixed';
  const release = meta?.baseline_release;
  const cutoff = meta?.knowledge_cutoff ?? release?.knowledge_cutoff ?? null;
  const vt = referenceTime ?? (meta?.valid_time ? validTimeLabel(meta.valid_time) : 'not stated');

  return (
    <div className={`context-banner${sim ? ' context-simulation' : ''}`} role="region" aria-label="Result context">
      {sim && (
        <p className="simulation-flag" data-testid="simulation-banner">
          <strong>SIMULATION</strong>
          {SIMULATION_BANNER_TEXT.replace(/^SIMULATION/, '')}
        </p>
      )}
      {fixture && (
        <p className="fixture-flag" data-testid="fixture-label">
          <strong>TEST FIXTURE DATA</strong>
          {origin.origin === 'mixed' ? ' — some records come from captured test fixtures' : ' — captured fixture records, not a live acquisition'}
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
        {origin.origin && (
          <div>
            <dt>Origin</dt>
            <dd data-testid="banner-origin" data-origin={origin.origin} data-origin-derived={origin.derived ? 'true' : 'false'}>
              {ORIGIN_TEXT[origin.origin] ?? String(origin.origin).replace(/_/g, ' ')}
              {origin.derived && <span className="muted"> (of the records shown on this page)</span>}
              {!origin.derived && origin.origin === 'none' && fixture && release?.origin === 'test_fixture' && (
                <span className="muted"> (none shown here; baseline release is a test fixture)</span>
              )}
            </dd>
          </div>
        )}
      </dl>
      {meta?.warnings?.some((w) => w.code === 'publication_paused') && (
        <p className="paused-flag" data-testid="publication-paused">
          <strong>PUBLICATION PAUSED</strong> — some values below are stale (the last accepted ones) until a reviewed connector repair lifts
          the pause. Stale rows are badged.
        </p>
      )}
      {meta?.warnings && meta.warnings.length > 0 && (
        <ul className="context-warnings" aria-label="Warnings">
          {meta.warnings.map((w, i) => (
            <li key={`${w.code}-${i}`} className={w.code === 'publication_paused' ? 'warning-paused' : undefined}>
              <strong>{w.code.replace(/_/g, ' ')}:</strong> {w.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
