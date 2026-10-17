import { missingReasonFr } from './format';

/**
 * A typed Missing value: hatched, with its reason ("manquant : non rapporté par la source").
 * Never 0, never blank, never a dash. `reason` is a provider/atlas code (translated) or French text.
 */
export function MissingValue({ reason, note, compact = false, block = false }: { reason?: string | null; note?: string; compact?: boolean; block?: boolean }) {
  const text = missingReasonFr(reason);
  return (
    <span
      className={`v8-missing${compact ? ' v8-missing-compact' : ''}${block ? ' v8-missing-block' : ''}`}
      data-missing-reason={reason ?? 'unstated'}
      title={note ? `${text} — ${note}` : `manquant : ${text}`}
      data-testid="v8-missing"
    >
      {compact ? 'manquant' : `manquant : ${text}`}
    </span>
  );
}
