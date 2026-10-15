import { Link } from 'react-router';
import { classifyV3Error } from '../api/common';
import { getAtlasConcepts, getAtlasCoverage } from '../api/atlas';
import { searchKg } from '../api/kg';
import { getCountryLinks } from '../api/links';
import { useApi } from '../../lib/useApi';
import { V3_SCREENS } from '../screens';

function Status({ name, state }: { name: string; state: { status: string; error?: unknown } }) {
  let text = 'vérification…';
  let cls = 'pending';
  if (state.status === 'ok') {
    text = 'disponible';
    cls = 'ok';
  } else if (state.status === 'error') {
    const info = classifyV3Error(state.error);
    text = info.kind === 'unavailable' ? 'pas encore disponible' : info.kind === 'no_data' ? 'aucune donnée chargée' : info.kind === 'not_found' ? 'aucune donnée pour l’exemple' : info.title.toLowerCase();
    cls = info.kind;
  }
  return (
    <li className={`v3-svc v3-svc-${cls}`}>
      <span className="v3-svc-dot" aria-hidden="true" />
      {name} : <strong>{text}</strong>
    </li>
  );
}

/** V3 landing: the screens and the live availability of each backend service (no data shown). */
export default function HomePage() {
  const kg = useApi((s) => searchKg('France', { limit: 1 }, s), []);
  const concepts = useApi((s) => getAtlasConcepts({ limit: 1 }, s), []);
  const coverage = useApi((s) => getAtlasCoverage({ domain: 'health' }, s), []);
  const links = useApi((s) => getCountryLinks('FRA', {}, s), []);
  return (
    <section className="v3-page v3-home" aria-labelledby="v3-home-title">
      <div className="v3-hero">
        <h1 id="v3-home-title">La planète, reliée comme des neurones</h1>
        <p>
          Chaque pays, chaque domaine, chaque époque : un graphe de connaissances sourcé (Wikidata et jeux de données ouverts), un atlas de tous les
          indicateurs, et un moteur qui explique <em>comment</em> deux choses sont reliées — étape par étape, chaque étape citée.
        </p>
      </div>
      <ul className="v3-cards">
        {V3_SCREENS.map((s) => (
          <li key={s.to}>
            <Link to={s.to} className="v3-card">
              <strong>{s.short}</strong>
              <span>{s.description}</span>
            </Link>
          </li>
        ))}
      </ul>
      <section aria-labelledby="v3-svc-title" className="v3-services">
        <h2 id="v3-svc-title">Services</h2>
        <ul>
          <Status name="Graphe de connaissances (recherche)" state={kg} />
          <Status name="Catalogue de l’atlas" state={concepts} />
          <Status name="Instantané de l’atlas" state={coverage} />
          <Status name="Liens entre pays" state={links} />
          <li className="v3-svc v3-svc-pending">
            <span className="v3-svc-dot" aria-hidden="true" />
            Questions (Demander) : <strong>vérifié à l’usage</strong>
          </li>
        </ul>
        <p className="field-hint">Un service « pas encore disponible » n’affiche rien à la place : aucune donnée n’est simulée.</p>
      </section>
      <section className="v3-rules" aria-labelledby="v3-rules-title">
        <h2 id="v3-rules-title">Règles d’honnêteté</h2>
        <ul>
          <li>Chaque déclaration garde sa source (élément, propriété, identifiant de déclaration, révision).</li>
          <li>Un chemin n’est pas une cause : « relié via » ; la causalité n’apparaît que si une source l’affirme, et elle lui est attribuée.</li>
          <li>Wikidata est collaboratif : « communautaire, non vérifié ».</li>
          <li>Une cellule absente n’est jamais un zéro ; les sources consultées sont listées.</li>
        </ul>
      </section>
    </section>
  );
}
