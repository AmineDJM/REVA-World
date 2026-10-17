import { useId, useState } from 'react';
import type { Footnote } from './api';

/** One footnote's lines in French (source, dataset, indicator, period, licence, method, status, reference). */
export function footnoteLines(f: Footnote): { k: string; v: string; href?: string | null }[] {
  const lic = f.licence ? `${f.licence}${f.non_commercial ? ' — usage non commercial' : f.restricted ? ' — conditions restrictives' : ''}` : 'non indiquée par la source';
  const out: { k: string; v: string; href?: string | null }[] = [{ k: 'Source', v: f.title, href: f.url }];
  if (f.dataset) out.push({ k: 'Jeu', v: f.dataset });
  if (f.indicator) out.push({ k: 'Série', v: f.indicator });
  if (f.period) out.push({ k: 'Période', v: f.period });
  out.push({ k: 'Licence', v: lic, href: f.licence_url });
  if (f.method) out.push({ k: 'Méthode', v: f.method });
  if (f.verification) out.push({ k: 'Statut', v: VERIF_FR[f.verification] ?? f.verification });
  if (f.retrieved) out.push({ k: 'Récupéré', v: f.retrieved });
  if (f.ref) out.push({ k: 'Réf.', v: f.ref });
  if (f.required_notice) out.push({ k: 'Mention', v: f.required_notice });
  return out;
}

const VERIF_FR: Record<string, string> = {
  community_edited_not_verified: 'édition communautaire, non vérifiée',
  source_reported_not_verified: 'déclaré par la source, non vérifié par REVA',
  derived: 'dérivé par REVA',
  published_estimate: 'estimation publiée, citée',
  sealed_forecast: 'prévision scellée',
  unvalidated_scenario: 'scénario non validé — pas une probabilité',
  preregistered_evaluation: 'évaluation pré-enregistrée',
};

/**
 * Footnote reference as a hover chip: superscript number; hover / focus shows the source card, a tap pins it
 * (phone), Escape closes. The number links to the full note in « Sources ».
 */
export function FnRef({ f, n }: { f: Footnote | undefined; n: number }) {
  const [pinned, setPinned] = useState(false);
  const id = useId();
  const nc = f?.non_commercial;
  return (
    <span className={`dos-fn${pinned ? ' dos-fn-pinned' : ''}`}>
      <button
        type="button"
        className={`dos-fn-chip${nc ? ' dos-fn-nc' : f?.restricted ? ' dos-fn-lic' : ''}`}
        aria-describedby={id}
        aria-label={`Note ${n}${f ? ` : ${f.title}` : ''}`}
        aria-expanded={pinned}
        data-fn={n}
        onClick={() => setPinned((p) => !p)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') setPinned(false);
        }}
      >
        {n}
      </button>
      <span className="dos-fn-pop" role="tooltip" id={id}>
        {f ? (
          <>
            {footnoteLines(f).map((l) => (
              <span key={l.k} className="dos-fn-row">
                <span className="dos-fn-k">{l.k}</span>
                <span className="dos-fn-v">
                  {l.href ? (
                    <a href={l.href} target="_blank" rel="noreferrer noopener">
                      {l.v}
                    </a>
                  ) : (
                    l.v
                  )}
                </span>
              </span>
            ))}
            <a className="dos-fn-all" href={`#fn-${n}`} onClick={() => setPinned(false)}>
              Voir la note {n} dans les sources
            </a>
          </>
        ) : (
          <span className="dos-fn-row">Note {n} absente du document</span>
        )}
      </span>
    </span>
  );
}
