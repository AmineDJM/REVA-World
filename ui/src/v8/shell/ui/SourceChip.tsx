import { useId, useState } from 'react';
import type { AtlasCell } from '../../../v3/api/atlas';
import type { Citation } from '../../../v3/lib/citations';
import { citeAtlasCell } from '../../../v3/lib/citations';
import { licenceTermsFr, type RightsLike } from '../../../v3/lib/licence';

export interface SourceInfo {
  /** Source key or short name ("worldbank_wdi", "IMF WEO", "wikidata"). */
  source: string;
  /** Text on the chip (defaults to the source). */
  label?: string;
  href?: string | null;
  /** Licence: the atlas `rights` object, or a plain licence name. */
  licence?: RightsLike | string | null;
  retrievedAt?: string | null;
  vintage?: string | null;
  period?: string | null;
  details?: string[];
  /** A v3 citation (citeAtlasCell / citeKgStatement / citeLinkEvidence) — its details are shown. */
  citation?: Citation | null;
}

/**
 * Source + licence of a value, always one hover/focus away. The chip is compact ("WDI · NC");
 * the popover shows the source, licence terms, attribution, vintage, retrieval date and provenance lines.
 * Tap/click pins the popover open (phone).
 */
export function SourceChip(props: SourceInfo) {
  const { source, label, href, licence, retrievedAt, vintage, period, details = [], citation } = props;
  const [pinned, setPinned] = useState(false);
  const id = useId();
  const rights: RightsLike | null = typeof licence === 'string' ? { licence } : (licence ?? null);
  const terms = licenceTermsFr(rights);
  const nc = terms.includes('licence non commerciale');
  const restricted = terms.length > 0;
  const lines = [...(citation?.details ?? []), ...details];
  const link = href ?? citation?.href ?? null;
  const community = citation?.community ?? false;
  const fixture = citation?.fixture ?? false;
  return (
    <span className={`v8-src${pinned ? ' v8-src-pinned' : ''}`} data-testid="v8-source">
      <button type="button" className="v8-src-chip" aria-describedby={id} aria-expanded={pinned} onClick={() => setPinned((p) => !p)} onKeyDown={(e) => e.key === 'Escape' && setPinned(false)}>
        <span className="v8-src-name">{label ?? citation?.label ?? source}</span>
        {restricted && <span className={`v8-src-flag${nc ? ' v8-src-nc' : ''}`}>{nc ? 'NC' : 'lic.'}</span>}
        {community && <span className="v8-src-flag v8-src-comm">comm.</span>}
        {fixture && <span className="v8-src-flag v8-src-fix">test</span>}
      </button>
      <span className="v8-src-pop" role="tooltip" id={id}>
        <span className="v8-src-row">
          <span className="v8-src-k">Source</span>
          <span className="v8-src-v">{link ? <a href={link} target="_blank" rel="noreferrer noopener">{source}</a> : source}</span>
        </span>
        {rights && (
          <span className="v8-src-row">
            <span className="v8-src-k">Licence</span>
            <span className="v8-src-v">
              {rights.licence_url ? <a href={rights.licence_url} target="_blank" rel="noreferrer noopener">{rights.licence}</a> : rights.licence}
              {terms.length > 0 && <strong className="v8-src-terms"> — {terms.join(' · ')}</strong>}
            </span>
          </span>
        )}
        {rights?.attribution && (
          <span className="v8-src-row">
            <span className="v8-src-k">Attribution</span>
            <span className="v8-src-v">{rights.attribution}</span>
          </span>
        )}
        {period && (
          <span className="v8-src-row">
            <span className="v8-src-k">Période</span>
            <span className="v8-src-v">{period}</span>
          </span>
        )}
        {vintage && (
          <span className="v8-src-row">
            <span className="v8-src-k">Millésime</span>
            <span className="v8-src-v">{vintage}</span>
          </span>
        )}
        {retrievedAt && (
          <span className="v8-src-row">
            <span className="v8-src-k">Récupéré</span>
            <span className="v8-src-v">{retrievedAt.slice(0, 10)}</span>
          </span>
        )}
        {community && <span className="v8-src-note">Base collaborative : déclarations non vérifiées par REVA.</span>}
        {fixture && <span className="v8-src-note">Donnée de test, pas une donnée publiée.</span>}
        {rights?.required_notice && (
          <span className="v8-src-note" lang="en">
            {rights.required_notice}
          </span>
        )}
        {lines.length > 0 && (
          <span className="v8-src-lines">
            {lines.map((l) => (
              <span key={l}>{l}</span>
            ))}
          </span>
        )}
      </span>
    </span>
  );
}

/** SourceChip props for an atlas cell (source/dataset, licence, period, raw artifact). */
export function atlasCellSource(cell: AtlasCell): SourceInfo {
  return {
    source: `${cell.source}/${cell.dataset}`,
    label: cell.source,
    licence: cell.rights,
    period: `${cell.period_start} → ${cell.period_end}`,
    citation: citeAtlasCell(cell),
  };
}
