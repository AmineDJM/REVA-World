import { missingReasonLabel } from '../lib/format';
import { UncertaintyGlyph } from './UncertaintyGlyph';

export interface MissingValueProps {
  reason?: string | null;
  /** Optional extra context, e.g. "Comtrade returned zero rows for 2023". */
  note?: string;
}

/**
 * Renders a missing value as "missing (reason)". A missing value is never
 * displayed as 0, a blank or a dash that could be read as zero.
 */
export function MissingValue({ reason, note }: MissingValueProps) {
  const label = missingReasonLabel(reason);
  return (
    <span className="missing-value" data-missing-reason={reason ?? 'unstated'} title={note}>
      <UncertaintyGlyph kind="missing" className="uglyph-inline" />
      missing ({label})
    </span>
  );
}
