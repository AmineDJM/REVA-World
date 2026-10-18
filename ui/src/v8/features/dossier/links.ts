/** Zod-free helpers shared by index.ts (first-load bundle) and the lazy page. */

/** Front-end route of a brief (one URL per brief). */
export function dossierHref(kind: string, id: string): string {
  return `/v8/dossier/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`;
}

export const KIND_LABEL: Record<string, string> = {
  country: 'Pays',
  commodity: 'Matière première',
  disease: 'Maladie',
  drug: 'Médicament',
  conflict: 'Conflit',
  decision: 'Décision',
};
