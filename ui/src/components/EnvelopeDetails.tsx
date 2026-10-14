import type { Assessment, EnvelopeMeta } from '../api/types';
import { formatDateTime, humanize, unitLabel } from '../lib/format';
import { JsonView } from './JsonView';
import { ProvenanceButton } from './ProvenanceDrawer';
import { UncertaintyPanel } from './UncertaintyPanel';

export function AssessmentView({ a }: { a: Assessment | null | undefined }) {
  if (!a) return <span className="muted">not reported</span>;
  if (a.status === 'not_applicable')
    return (
      <span>
        Not applicable{a.reason ? <span className="muted"> — {a.reason}</span> : null}
      </span>
    );
  if (a.status === 'not_assessed')
    return (
      <span>
        Not assessed{a.reason ? <span className="muted"> — {a.reason}</span> : null}
      </span>
    );
  return (
    <span>
      {a.reason && <span>{a.reason} </span>}
      <JsonView value={a.detail} emptyLabel="reported (no detail)" />
    </span>
  );
}

export interface EnvelopeDetailsProps {
  meta: Partial<EnvelopeMeta> | null | undefined;
  /** Open by default (e.g. simulation results). */
  open?: boolean;
  title?: string;
  maxLinks?: number;
}

/**
 * The explanation fields every result carries: units, uncertainty,
 * assumptions, applicability, model versions, historical validation,
 * freshness and provenance links.
 */
export function EnvelopeDetails({ meta, open = false, title = 'About this result', maxLinks = 12 }: EnvelopeDetailsProps) {
  if (!meta) return null;
  const links = meta.provenance_links ?? [];
  return (
    <details className="envelope-details" open={open}>
      <summary>{title}</summary>
      <dl className="kv">
        <div className="kv-row">
          <dt>Units</dt>
          <dd>{meta.units && meta.units.length ? meta.units.map((u) => `${unitLabel(u)} (${u})`).join(', ') : 'not stated'}</dd>
        </div>
        {meta.estimand && (
          <div className="kv-row">
            <dt>Estimand</dt>
            <dd>
              <JsonView value={meta.estimand} />
            </dd>
          </div>
        )}
        <div className="kv-row">
          <dt>Uncertainty</dt>
          <dd>
            <UncertaintyPanel uncertainty={meta.uncertainty} compact />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Assumptions</dt>
          <dd>
            {meta.assumptions && meta.assumptions.length ? (
              <ul className="json-list">
                {meta.assumptions.map((a) => (
                  <li key={a}>{a}</li>
                ))}
              </ul>
            ) : (
              'none stated'
            )}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Applicability</dt>
          <dd>
            <AssessmentView a={meta.applicability} />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Model versions</dt>
          <dd>
            {meta.model_versions && meta.model_versions.length
              ? meta.model_versions.map((m) => `${m.model_key}@${m.version}${m.approval_status ? ` (${humanize(m.approval_status)})` : ''}`).join(', ')
              : 'none'}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Historical validation</dt>
          <dd>
            <AssessmentView a={meta.historical_validation} />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Evidence profile</dt>
          <dd>
            <AssessmentView a={meta.evidence_profile} />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Model disagreement</dt>
          <dd>
            <AssessmentView a={meta.model_disagreement} />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Numerical error</dt>
          <dd>
            <AssessmentView a={meta.numerical_error} />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Compression error</dt>
          <dd>
            <AssessmentView a={meta.compression_error} />
          </dd>
        </div>
        <div className="kv-row">
          <dt>Source freshness</dt>
          <dd>
            {meta.data_freshness
              ? `retrieved ${formatDateTime(meta.data_freshness.latest_retrieved_at)}; recorded ${formatDateTime(meta.data_freshness.latest_recorded_at)}${meta.data_freshness.note ? ` — ${meta.data_freshness.note}` : ''}`
              : 'not reported'}
          </dd>
        </div>
        <div className="kv-row">
          <dt>Provenance</dt>
          <dd>
            {links.length === 0
              ? 'no links'
              : links.slice(0, maxLinks).map((l) => (
                  <span key={`${l.kind}:${l.id}`} className="prov-link">
                    <ProvenanceButton kind={l.kind} id={l.id} label={`${humanize(l.kind)} ${l.id.slice(0, 8)}`} />{' '}
                  </span>
                ))}
            {links.length > maxLinks && <span className="muted"> +{links.length - maxLinks} more</span>}
          </dd>
        </div>
        {meta.cost && (
          <div className="kv-row">
            <dt>Cost</dt>
            <dd>
              {meta.cost.wall_time_ms.toFixed(0)} ms, {meta.cost.rows} rows
            </dd>
          </div>
        )}
      </dl>
    </details>
  );
}
