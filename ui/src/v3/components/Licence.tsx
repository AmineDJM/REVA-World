/** Licence badges (review V3b LIC-1): "licence non commerciale", share-alike, no-derivatives, not open. */
import type { AtlasLicence, AtlasRights } from '../api/atlas';
import { licenceTermsFr, restrictedLicences } from '../lib/licence';

export function LicenceBadge({ rights }: { rights: AtlasRights | null | undefined }) {
  const terms = licenceTermsFr(rights);
  if (!rights || terms.length === 0) return null;
  return (
    <span className="v3-badge v3-badge-licence" title={`${rights.licence}${rights.attribution ? ` — ${rights.attribution}` : ''}`} data-testid="licence-badge">
      {terms.join(' · ')}
    </span>
  );
}

/** The restricted licences of an answer's sources, each named with its terms (empty when all are open). */
export function LicenceList({ licences, title = 'Licences restrictives' }: { licences: readonly AtlasLicence[]; title?: string }) {
  const bad = restrictedLicences(licences);
  if (bad.length === 0) return null;
  return (
    <div className="v3-licences" data-testid="licence-list">
      <h3>{title}</h3>
      <ul>
        {bad.map((l) => (
          <li key={`${l.source}|${l.licence}`}>
            <code>{l.source}</code> : {l.licence_url ? <a href={l.licence_url}>{l.licence}</a> : l.licence} <LicenceBadge rights={l} />
            {l.attribution && <span className="muted"> — attribution : {l.attribution}</span>}
          </li>
        ))}
      </ul>
      <p className="field-hint">Toute réutilisation de ces valeurs doit respecter ces conditions (par exemple : pas d’usage commercial).</p>
    </div>
  );
}
