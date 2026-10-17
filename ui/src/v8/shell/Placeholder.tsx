import { Link } from 'react-router';
import type { DiscoveredFeature } from './discovery';
import { prefetchElement } from './prefetch';
import { EmptyState } from './ui/States';
import { KeyHint } from './ui/KeyHint';

function FeatureCards({ features }: { features: readonly DiscoveredFeature[] }) {
  return (
    <ul className="v8-cards">
      {features.map((f) => (
        <li key={f.id}>
          <Link to={f.navTo!} className="v8-card" onMouseEnter={() => prefetchElement(f.element)} onFocus={() => prefetchElement(f.element)}>
            <span className="v8-card-icon" aria-hidden="true">
              {f.icon ?? f.title.slice(0, 1)}
            </span>
            <span className="v8-card-title">{f.title}</span>
            {f.goKey && <KeyHint keys={`g ${f.goKey}`} />}
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** /v8 when no feature claims the index route: the search door + what exists. */
export function BuiltinHome({ features, onOpenPalette }: { features: readonly DiscoveredFeature[]; onOpenPalette: () => void }) {
  return (
    <div className="v8-page v8-home">
      <header className="v8-page-head">
        <div>
          <h1 className="v8-page-title">Console REVA</h1>
          <p className="v8-page-lede">Le monde sourcé : chaque valeur avec sa source, chaque manque nommé, chaque prévision jugée.</p>
        </div>
      </header>
      <button type="button" className="v8-home-search" onClick={onOpenPalette}>
        <span>Rechercher une entité, un pays, un indicateur, une commande…</span>
        <KeyHint keys="Mod K" />
      </button>
      {features.length > 0 ? (
        <FeatureCards features={features} />
      ) : (
        <EmptyState title="Aucune section n’est encore installée">Les sections de la console apparaissent ici dès qu’elles sont livrées (dossier ui/src/v8/features/).</EmptyState>
      )}
    </div>
  );
}

/** Unknown /v8/… URL (or a section not delivered yet). */
export function NotFound({ features, pathname }: { features: readonly DiscoveredFeature[]; pathname: string }) {
  return (
    <div className="v8-page">
      <EmptyState title="Cette adresse ne correspond à aucune section de la console" action={<Link to="/v8">Retour à la console</Link>}>
        <p>
          <code>{pathname}</code> : la section n’existe pas ou n’est pas encore livrée. Rien n’est affiché à la place.
        </p>
      </EmptyState>
      {features.length > 0 && <FeatureCards features={features} />}
    </div>
  );
}
