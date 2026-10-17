/** Evidence item (NLQA) → SourceChip props: source, licence, period and provenance lines, never invented. */
import type { SourceInfo } from '../../shell/ui';
import type { Citation } from '../../../v3/lib/citations';
import type { RightsLike } from '../../../v3/lib/licence';
import type { Evidence } from './api';

type Rec = Record<string, unknown>;
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : typeof v === 'number' ? String(v) : null);

const KIND_FR: Record<string, string> = {
  atlas_cell: 'valeur de l’atlas',
  kg_statement: 'déclaration du graphe',
  causal_claim: 'affirmation causale attribuée',
  effect_estimate: 'estimation publiée',
  sealed_forecast: 'prévision scellée',
  decision_scenario: 'scénario du moteur',
  decision_hop: 'maillon du scénario',
  entity_identification: 'identification d’entité',
  registry_term: 'terme de registre',
  absence: 'absence (sources consultées)',
  derived_link: 'lien dérivé',
};

export function evidenceKindFr(kind: string): string {
  return KIND_FR[kind] ?? kind;
}

function rightsOf(c: Rec): RightsLike | string | null {
  const r = c.rights;
  if (r && typeof r === 'object' && typeof (r as Rec).licence === 'string') return r as unknown as RightsLike;
  if (typeof r === 'string') return r;
  return null;
}

export function evidenceSource(e: Evidence): SourceInfo {
  const c = (e.citation ?? {}) as Rec;
  const ev = e as unknown as Rec;
  const src = str(c.source) ?? e.kind;
  const ds = str(c.dataset);
  const details: string[] = [evidenceKindFr(e.kind)];
  if (str(c.indicator)) details.push(`indicateur ${str(c.indicator)}`);
  if (str(ev.period_start)) details.push(`période ${str(ev.period_start)} → ${str(ev.period_end) ?? ''}`);
  if (str(c.statement_id)) details.push(`déclaration ${str(c.statement_id)}`);
  if (str(c.tier)) details.push(`niveau de source ${str(c.tier)}`);
  if (str(ev.grade)) details.push(`preuve ${str(ev.grade)}`);
  const ref = c.reference;
  if (ref && typeof ref === 'object' && str((ref as Rec).short)) details.push(str((ref as Rec).short) as string);
  if (str(c.locator)) details.push(`localisation : ${str(c.locator)}`);
  if (str(c.quote)) details.push(`extrait : « ${(str(c.quote) as string).slice(0, 220)} »`);
  if (str(c.raw_sha256)) details.push(`artefact brut sha256 ${(str(c.raw_sha256) as string).slice(0, 12)}…`);
  if (str(c.atlas_snapshot_sha256)) details.push(`instantané atlas ${(str(c.atlas_snapshot_sha256) as string).slice(0, 12)}…`);
  if (c.outside_snapshot) details.push(`table enregistrée hors instantané : ${str(c.dataset_key)} (${(str(c.dataset_registered_at) ?? '').slice(0, 10)})`);
  if (str(c.batch_sha256)) details.push(`lot scellé ${(str(c.batch_sha256) as string).slice(0, 12)}… le ${(str(c.issued_at) ?? '').slice(0, 10)}`);
  if (str(c.status)) details.push(`statut : ${str(c.status)}`);
  const community = /wikidata/i.test(src) || str(c.status) === 'community_edited';
  const fixture = str(c.origin) === 'test_fixture' || str(ev.origin) === 'test_fixture' || /^fixture/.test(src);
  const citation: Citation = { label: ds ? `${src}/${ds}` : src, source: src, href: null, revision: str(c.revision), revisionHref: null, community, fixture, details };
  return {
    source: ds ? `${src}/${ds}` : src,
    label: `${e.id} · ${src}`,
    licence: rightsOf(c),
    retrievedAt: str(c.retrieved_at),
    citation,
  };
}
