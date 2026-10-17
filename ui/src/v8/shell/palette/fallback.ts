/** Fallback search (when /v1/v8/search is absent): /v1/kg/search + the atlas catalogue, filtered here. */
import { entityLabel, searchKg } from '../../../v3/api/kg';
import { getAtlasCatalog } from '../../../v3/api/atlas';
import { v8Links, type PaletteHit } from '../../contract';
import { bestScore } from './fuzzy';

const KIND_FR: Record<string, string> = {
  country: 'Pays',
  region: 'Région',
  person: 'Personne',
  organization: 'Organisation',
  company: 'Entreprise',
  event: 'Événement',
  drug: 'Médicament',
  gene: 'Gène',
  protein: 'Protéine',
  disease: 'Maladie',
  taxon: 'Taxon',
  element: 'Élément',
  material: 'Matériau',
  commodity: 'Produit de base',
  place: 'Lieu',
};

export async function fallbackSearch(q: string, signal: AbortSignal): Promise<PaletteHit[]> {
  const [kg, catalog] = await Promise.allSettled([searchKg(q, { limit: 10 }, signal), getAtlasCatalog()]);
  if (signal.aborted) throw new DOMException('aborted', 'AbortError');
  const out: PaletteHit[] = [];
  if (kg.status === 'fulfilled') {
    for (const h of kg.value.data.results) {
      const kind = h.kind ?? 'entity';
      out.push({
        id: `kg:${h.qid}`,
        label: entityLabel(h),
        sublabel: [KIND_FR[kind] ?? kind, h.qid, h.origin === 'test_fixture' ? 'donnée de test' : null].filter(Boolean).join(' · '),
        href: v8Links.entity(kind, h.qid),
        group: kind === 'country' || kind === 'region' ? 'Pays & régions' : 'Entités',
      });
    }
  }
  if (catalog.status === 'fulfilled') {
    const scored = catalog.value.data
      .map((c) => ({ c, s: bestScore(q, [c.labels.fr, c.labels.en, c.id]) }))
      .filter((x): x is { c: (typeof x)['c']; s: number } => x.s !== null && x.s >= 600)
      .sort((a, b) => b.s - a.s)
      .slice(0, 8);
    for (const { c } of scored) {
      out.push({
        id: `concept:${c.id}`,
        label: c.labels.fr || c.labels.en || c.id,
        sublabel: [c.domain, c.unit].filter(Boolean).join(' · '),
        href: v8Links.series(c.source_order[0] ?? c.id, { concept: c.id }),
        group: 'Indicateurs',
      });
    }
  }
  if (kg.status === 'rejected' && catalog.status === 'rejected') throw kg.reason;
  return out;
}
