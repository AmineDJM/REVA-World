/**
 * V3 screens: the single nav entry and the command-palette entries. Kept free of heavy imports
 * (statically imported by App.tsx); the screens themselves are lazy chunks under ./V3App.
 */
export const V3_NAV = { to: '/v3', label: 'Planète' };

export interface V3Screen {
  to: string;
  label: string;
  short: string;
  description: string;
}

export const V3_SCREENS: V3Screen[] = [
  { to: '/v3/neurones', short: 'Neurones', label: 'Neurones — explorer le graphe du monde', description: 'Partir d’une entité et déplier ses connexions sourcées.' },
  { to: '/v3/chemin', short: 'Chemin', label: 'Chemin — comment X est-il lié à Y ?', description: 'Chaînes de déclarations entre deux entités ; un chemin n’est pas une cause.' },
  { to: '/v3/frise', short: 'Frise', label: 'Frise — chronologie d’une entité', description: 'Événements, appartenances, chefs d’État, des siècles aux années.' },
  { to: '/v3/atlas', short: 'Atlas', label: 'Atlas — carte mondiale de toute variable', description: 'Choroplèthe par année, manques hachurés avec leur raison, classement.' },
  { to: '/v3/pays/FRA', short: 'Dossier pays', label: 'Dossier pays — tous les domaines d’un pays', description: 'Géographie, population, économie, travail, santé, droit, culture, histoire, liens.' },
  { to: '/v3/relation?a=BRA&b=PRT', short: 'Pays ↔ Pays', label: 'Pays ↔ Pays — tous les liens entre deux pays', description: 'Commerce, migrations, votes à l’ONU, colonisation, frontières, langues, traités, conflits — dans le temps, chaque lien cité.' },
  { to: '/v3/causes', short: 'Causes', label: 'Causes & conséquences — qui affirme quelle cause ?', description: 'Affirmations causales sourcées et graduées, « pourquoi ? », chaînes, désaccords ; jamais une causalité établie par REVA.' },
  { to: '/v3/demander', short: 'Demander', label: 'Demander — question en langage naturel', description: 'Réponse construite uniquement à partir de sources citées.' },
];

/** Entries for the ⌘K palette (screens). */
export const V3_PALETTE = [
  ...V3_SCREENS.map((s) => ({ to: s.to, label: s.label })),
  { to: '/v3/chemin?from=Q7322&to=Q155', label: 'Chemin — exemple : Christophe Colomb → Brésil' },
  { to: '/v3/neurones?qid=Q155', label: 'Neurones — exemple : Brésil' },
  { to: '/v3/pays/BRA', label: 'Dossier pays — Brésil' },
  { to: '/v3/pays/USA', label: 'Dossier pays — États-Unis' },
  { to: '/v3/relation?a=BRA&b=PRT', label: 'Pays ↔ Pays — Brésil et Portugal' },
  { to: '/v3/relation?a=DZA&b=FRA', label: 'Pays ↔ Pays — Algérie et France' },
  { to: '/v3/causes?event=Q361', label: 'Causes & conséquences — Première Guerre mondiale' },
];
