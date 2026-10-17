/**
 * Entity kinds of the knowledge graph grouped for display (colour + shape, so colour is never the
 * only cue). KG kinds come from the query specs (registry/kg/queries/**: country, historical_state,
 * empire, colony, person, war, treaty, voyage, organisation, language, religion, event, era, ...).
 */
export type KindGroup = 'state' | 'empire' | 'person' | 'event' | 'language' | 'religion' | 'organisation' | 'place' | 'era' | 'other';

export interface KindStyle {
  group: KindGroup;
  label: string;
  color: string;
  /** Cytoscape node shape. */
  shape: string;
}

export const KIND_STYLES: Record<KindGroup, KindStyle> = {
  state: { group: 'state', label: 'État', color: '#4f9dff', shape: 'round-rectangle' },
  empire: { group: 'empire', label: 'Empire', color: '#b388ff', shape: 'octagon' },
  person: { group: 'person', label: 'Personne', color: '#ffb74d', shape: 'ellipse' },
  event: { group: 'event', label: 'Événement', color: '#ff6b6b', shape: 'diamond' },
  language: { group: 'language', label: 'Langue', color: '#4dd0a1', shape: 'triangle' },
  religion: { group: 'religion', label: 'Religion', color: '#f48fb1', shape: 'star' },
  organisation: { group: 'organisation', label: 'Organisation', color: '#26c6da', shape: 'rectangle' },
  place: { group: 'place', label: 'Lieu', color: '#9ccc65', shape: 'tag' },
  era: { group: 'era', label: 'Époque', color: '#e6c86e', shape: 'barrel' },
  other: { group: 'other', label: 'Autre / non chargé', color: '#9aa4b2', shape: 'ellipse' },
};

const GROUP_OF: Record<string, KindGroup> = {
  country: 'state',
  state: 'state',
  sovereign_state: 'state',
  historical_state: 'state',
  historical_country: 'state',
  colony: 'state',
  protectorate: 'state',
  kingdom: 'state',
  polity: 'state',
  dependent_territory: 'state',
  empire: 'empire',
  person: 'person',
  human: 'person',
  event: 'event',
  war: 'event',
  battle: 'event',
  conflict: 'event',
  treaty: 'event',
  voyage: 'event',
  expedition: 'event',
  revolution: 'event',
  election: 'event',
  movement: 'event',
  resolution: 'event',
  language: 'language',
  language_family: 'language',
  dialect: 'language',
  religion: 'religion',
  religious_denomination: 'religion',
  organisation: 'organisation',
  organization: 'organisation',
  international_organisation: 'organisation',
  international_organization: 'organisation',
  alliance: 'organisation',
  party: 'organisation',
  place: 'place',
  city: 'place',
  region: 'place',
  territory: 'place',
  heritage_site: 'place',
  river: 'place',
  continent: 'place',
  era: 'era',
  period: 'era',
  dynasty: 'era',
};

export function kindGroup(kind: string | null | undefined): KindGroup {
  if (!kind) return 'other';
  return GROUP_OF[kind] ?? GROUP_OF[kind.replace(/s$/, '')] ?? 'other';
}

export function kindStyle(kind: string | null | undefined): KindStyle {
  return KIND_STYLES[kindGroup(kind)];
}

const KIND_LABEL_FR: Record<string, string> = {
  country: 'pays',
  historical_state: 'État historique',
  colony: 'colonie',
  empire: 'empire',
  person: 'personne',
  war: 'guerre',
  treaty: 'traité',
  voyage: 'voyage',
  event: 'événement',
  language: 'langue',
  language_family: 'famille de langues',
  religion: 'religion',
  organisation: 'organisation',
  movement: 'mouvement',
  heritage_site: 'site du patrimoine',
  era: 'époque',
  place: 'lieu',
  other: 'autre',
  // kinds of the event / causal records loaded by the V3 dataset agents (ICB, MID, UCDP, NAVCO, COW, IDMC, NOAA…)
  international_crisis: 'crise internationale',
  foreign_policy_crisis: 'crise de politique étrangère',
  crisis_trigger: 'déclencheur de crise (codé)',
  crisis_response: 'réponse à une crise (codée)',
  crisis_outcome: 'issue de crise (codée)',
  militarized_interstate_dispute: 'différend interétatique militarisé',
  dispute_outcome: 'issue de différend (codée)',
  armed_conflict_episode: 'épisode de conflit armé',
  conflict_termination: 'fin de conflit (codée)',
  campaign_outcome: 'issue de campagne (codée)',
  war_outcome: 'issue de guerre (codée)',
  coded_outcome: 'issue codée',
  sanction_episode: 'épisode de sanctions',
  disaster_event: 'catastrophe',
  disaster_displacement: 'déplacements dus à une catastrophe',
  tsunami: 'tsunami',
  earthquake: 'séisme',
  volcanic_eruption: 'éruption volcanique',
  condition: 'condition',
  unclassified: 'non classé',
};

/** French label of a raw KG kind ("historical_state" -> "État historique"). */
export function kindLabel(kind: string | null | undefined): string {
  if (!kind) return 'type non chargé';
  return KIND_LABEL_FR[kind] ?? kind.replace(/_/g, ' ');
}
