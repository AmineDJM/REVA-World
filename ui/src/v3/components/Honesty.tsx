/** V3 honesty labels (docs/roadmap/v3_world_graph.md, "Honesty rules"). */
import { COMMUNITY_BADGE } from '../lib/citations';
import { PATH_NOT_CAUSE } from '../lib/paths';
import { missingReasonFr } from '../lib/format';

export function CommunityBadge() {
  return (
    <span className="v3-badge v3-badge-community" title="Wikidata est une base collaborative ; REVA ne vérifie pas ces déclarations." data-testid="community-badge">
      {COMMUNITY_BADGE}
    </span>
  );
}

export function FixtureBadge() {
  return (
    <span className="v3-badge v3-badge-fixture" title="Donnée de test (origin = test_fixture), pas une donnée réelle publiée.">
      donnée de test
    </span>
  );
}

export function ProviderDeclaredBadge() {
  return (
    <span className="v3-badge v3-badge-declared" title="Concept défini par le fournisseur, non relu par un humain chez REVA.">
      défini par le fournisseur, non relu
    </span>
  );
}

export function PathNotCauseBanner({ text }: { text?: string }) {
  return (
    <p className="v3-banner v3-banner-path" role="note" data-testid="path-not-cause">
      <strong>Un chemin n’est pas une cause.</strong> {text && text !== PATH_NOT_CAUSE ? text : 'Chaque étape dit seulement « relié via » une déclaration sourcée ; la formulation causale n’apparaît que si une source l’affirme, et elle lui est attribuée.'}
    </p>
  );
}

export function SourcedOnlyBanner() {
  return (
    <p className="v3-banner v3-banner-sourced" role="note" data-testid="sourced-only">
      <strong>Réponse construite uniquement à partir de sources citées.</strong> Chaque phrase renvoie à ses preuves ; une donnée absente est dite absente,
      jamais devinée. Aucune sortie de modèle de langage n’est présentée comme un fait.
    </p>
  );
}

/** A missing atlas cell: "manquant (raison)". Never 0, never blank, never a dash. */
export function MissingCell({ reason, note }: { reason?: string | null; note?: string }) {
  return (
    <span className="v3-missing" data-missing-reason={reason ?? 'unstated'} title={note}>
      <span className="v3-missing-glyph" aria-hidden="true" />
      manquant ({missingReasonFr(reason)})
    </span>
  );
}
