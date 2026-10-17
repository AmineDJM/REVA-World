import { useEffect, useRef } from 'react';
import type { DiscoveredFeature } from './discovery';
import { KeyHint } from './ui/KeyHint';

export function ShortcutsSheet({ open, onClose, features, problems }: { open: boolean; onClose: () => void; features: readonly DiscoveredFeature[]; problems: readonly string[] }) {
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (open) ref.current?.focus();
  }, [open]);
  if (!open) return null;
  const go = features.filter((f) => f.goKey && f.navTo);
  const rows: [string, string][] = [
    ['Mod K', 'Palette : rechercher, aller à, commandes'],
    ['/', 'Palette (hors d’un champ de saisie)'],
    ['?', 'Cette aide'],
    ['[', 'Replier / déplier la navigation'],
    ['Escape', 'Fermer'],
  ];
  const pal: [string, string][] = [
    ['ArrowUp ArrowDown', 'Résultat précédent / suivant'],
    ['Enter', 'Ouvrir'],
    ['Mod Enter', 'Ouvrir dans un nouvel onglet'],
  ];
  return (
    <div className="v8-pal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()} onKeyDown={(e) => (e.key === 'Escape' || e.key === '?') && (e.preventDefault(), onClose())}>
      <div className="v8-help" role="dialog" aria-modal="true" aria-labelledby="v8-help-title">
        <header className="v8-help-head">
          <h2 id="v8-help-title">Raccourcis clavier</h2>
          <button type="button" ref={ref} className="v8-btn v8-btn-ghost v8-btn-sm" onClick={onClose} aria-label="Fermer l’aide">
            <KeyHint keys="Escape" />
          </button>
        </header>
        <div className="v8-help-cols">
          <section>
            <h3>Partout</h3>
            <dl>
              {rows.map(([k, t]) => (
                <div key={k} className="v8-help-row">
                  <dt>
                    <KeyHint keys={k} />
                  </dt>
                  <dd>{t}</dd>
                </div>
              ))}
            </dl>
            <h3>Dans la palette</h3>
            <dl>
              {pal.map(([k, t]) => (
                <div key={k} className="v8-help-row">
                  <dt>
                    <KeyHint keys={k} />
                  </dt>
                  <dd>{t}</dd>
                </div>
              ))}
            </dl>
          </section>
          <section>
            <h3>Aller à</h3>
            {go.length === 0 ? (
              <p className="v8-muted">Aucune section n’a encore déclaré de raccourci.</p>
            ) : (
              <dl>
                {go.map((f) => (
                  <div key={f.id} className="v8-help-row">
                    <dt>
                      <KeyHint keys={`g ${f.goKey}`} />
                    </dt>
                    <dd>{f.title}</dd>
                  </div>
                ))}
              </dl>
            )}
          </section>
        </div>
        {problems.length > 0 && (
          <details className="v8-help-problems">
            <summary>Sections ignorées ou en conflit ({problems.length})</summary>
            <ul>
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}
