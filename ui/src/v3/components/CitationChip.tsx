import { useId, useState } from 'react';
import type { Citation } from '../lib/citations';
import { CommunityBadge, FixtureBadge } from './Honesty';

/**
 * A compact citation: source + statement id (+ revision), linking to the statement on Wikidata
 * when that link can be built, with the community / fixture badges and an expandable provenance.
 */
export function CitationChip({ c, compact = false }: { c: Citation; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <span className="v3-cite" data-testid="citation">
      <span className="v3-cite-main">
        {c.href ? (
          <a href={c.href} target="_blank" rel="noreferrer noopener" title="Ouvrir la déclaration à la source">
            {c.label}
          </a>
        ) : (
          <span>{c.label}</span>
        )}
        {c.revisionHref && (
          <a className="v3-cite-rev" href={c.revisionHref} target="_blank" rel="noreferrer noopener" title={`Révision ${c.revision} de la source`}>
            révision
          </a>
        )}
        {c.community && <CommunityBadge />}
        {c.fixture && <FixtureBadge />}
        {!compact && c.details.length > 0 && (
          <button type="button" className="v3-cite-more" aria-expanded={open} aria-controls={id} onClick={() => setOpen((o) => !o)} title="Provenance détaillée">
            {open ? '−' : '+'}
            <span className="sr-only"> provenance</span>
          </button>
        )}
      </span>
      {open && (
        <ul className="v3-cite-details" id={id}>
          {c.details.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
      )}
    </span>
  );
}

export function CitationList({ items }: { items: Citation[] }) {
  if (items.length === 0) return <span className="v3-missing">aucune citation fournie</span>;
  return (
    <span className="v3-cite-list">
      {items.map((c, i) => (
        <CitationChip key={`${c.label}-${i}`} c={c} />
      ))}
    </span>
  );
}
