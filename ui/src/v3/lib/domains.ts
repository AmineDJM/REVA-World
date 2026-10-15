/** Atlas domains (registry/atlas/catalog/<domain>.yaml) in dossier order, with French titles. */
export const DOMAIN_ORDER: { id: string; label: string; aliases?: string[] }[] = [
  { id: 'geography', label: 'Géographie' },
  { id: 'population', label: 'Population', aliases: ['demography'] },
  { id: 'economy', label: 'Économie', aliases: ['imf', 'trade', 'macro'] },
  { id: 'labour', label: 'Travail et salaires', aliases: ['labor', 'wages'] },
  { id: 'quality_of_life', label: 'Société et qualité de vie', aliases: ['social', 'society'] },
  { id: 'health', label: 'Santé' },
  { id: 'education', label: 'Éducation' },
  { id: 'science', label: 'Science et innovation' },
  { id: 'politics', label: 'Politique et gouvernance', aliases: ['governance', 'geopolitics'] },
  { id: 'law', label: 'Droit et lois' },
  { id: 'culture', label: 'Culture, langues et religions' },
  { id: 'energy', label: 'Énergie' },
  { id: 'environment', label: 'Environnement et climat', aliases: ['climate'] },
  { id: 'history', label: 'Histoire (séries longues)' },
];

const LABEL = new Map<string, string>();
const RANK = new Map<string, number>();
DOMAIN_ORDER.forEach((d, i) => {
  for (const k of [d.id, ...(d.aliases ?? [])]) {
    LABEL.set(k, d.id === k ? d.label : `${d.label} (${k === 'imf' ? 'FMI' : k})`);
    RANK.set(k, i);
  }
});

export function domainLabel(id: string): string {
  return LABEL.get(id) ?? id.replace(/_/g, ' ');
}

export function domainRank(id: string): number {
  return RANK.get(id) ?? DOMAIN_ORDER.length;
}
