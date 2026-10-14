import { isRecord } from '../lib/format';
import { MissingValue } from './MissingValue';

/**
 * Compact key/value rendering for loosely typed envelope fields
 * (applicability, assumptions, freshness). Null renders as "not reported".
 */
export function JsonView({ value, emptyLabel = 'not reported' }: { value: unknown; emptyLabel?: string }) {
  if (value === null || value === undefined) return <span className="muted">{emptyLabel}</span>;
  if (isRecord(value) && value.kind === 'missing') return <MissingValue reason={String(value.reason ?? '')} />;
  if (isRecord(value) && value.missing === 'not_extracted') return <MissingValue reason="not_extracted" />;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">none</span>;
    if (value.every((v) => typeof v !== 'object' || v === null))
      return <span>{value.map((v) => String(v)).join(', ')}</span>;
    return (
      <ul className="json-list">
        {value.map((v, i) => (
          <li key={i}>
            <JsonView value={v} />
          </li>
        ))}
      </ul>
    );
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) return <span className="muted">none</span>;
    return (
      <dl className="kv">
        {entries.map(([k, v]) => (
          <div key={k} className="kv-row">
            <dt>{k.replace(/_/g, ' ')}</dt>
            <dd>
              <JsonView value={v} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <span>{String(value)}</span>;
}
