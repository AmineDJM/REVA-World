/**
 * Live reads behind board items. A board stores references only; values come from these existing routes:
 *   series   → GET /v1/atlas/headline/series (headline concept) or GET /v1/atlas/series (catalog concept)
 *   entity   → GET /v1/kg/entities/{id} (KG ids only; ISO3 countries link to the Fiche without a read)
 *   forecast → GET /v1/forecasts/questions/{id} (sealed forecasts, seal re-verified, outcome, scores)
 *   decision → GET /v1/predict/decisions (reference decisions; the propagation itself runs in Décision)
 * Each read is normalised to a small view the cards render; values stay decimal strings.
 */
import { z } from 'zod';
import { AtlasLicence, getAtlasSeries } from '../../../v3/api/atlas';
import { DecimalString, memo, v3get } from '../../../v3/api/common';
import { fetchDecisions } from '../../../v5/decision/api';

export interface SeriesPoint {
  year: number;
  value: string | null;
  projection: boolean;
  source: string; // answering series / source id
  missingReason: string | null;
}

export interface SeriesView {
  label: string;
  unit: string;
  points: SeriesPoint[];
  licences: z.output<typeof AtlasLicence>[];
  sourcesWithoutData: string[];
  breaks: number[]; // years where the answering source changes
  snapshot: string | null;
}

const HeadlineCell = z.looseObject({
  year: z.number().int(),
  value: DecimalString.nullable(),
  unit: z.string().nullish(),
  projection: z.boolean().default(false),
  missing_reason: z.string().nullish(),
  answered_by: z.looseObject({ series: z.string(), source: z.string().nullish() }).nullish(),
});

const HeadlineSeries = z.looseObject({
  data: z.looseObject({
    concept: z.looseObject({ id: z.string(), labels: z.record(z.string(), z.string()).default({}), unit: z.string().nullish() }),
    best: z.array(HeadlineCell),
    sources_without_data: z.array(z.string()).default([]),
    source_breaks: z.array(z.looseObject({ year: z.number() })).default([]),
    licences: z.array(AtlasLicence).nullish().transform((v) => v ?? []),
  }),
  snapshot: z.looseObject({ snapshot_sha256: z.string() }).nullish(),
});

export async function readSeries(concept: string, geo: string, headline: boolean, signal?: AbortSignal): Promise<SeriesView> {
  if (headline) {
    // projections included so they are drawn dashed and labelled; they never answer "latest value"
    const r = await v3get('/v1/atlas/headline/series', HeadlineSeries, { concept, iso3: geo, projections: 'include' }, signal);
    const d = r.data;
    const points = d.best
      .map((c) => ({
        year: c.year,
        value: c.value,
        projection: c.projection,
        source: c.answered_by?.series ?? '?',
        missingReason: c.value === null ? (c.missing_reason ?? null) : null,
      }))
      .sort((a, b) => a.year - b.year);
    return {
      label: d.concept.labels.fr ?? d.concept.labels.en ?? concept,
      unit: d.concept.unit ?? d.best[0]?.unit ?? '',
      points,
      licences: d.licences,
      sourcesWithoutData: d.sources_without_data,
      breaks: d.source_breaks.map((b) => b.year),
      snapshot: r.snapshot?.snapshot_sha256 ?? null,
    };
  }
  const r = await getAtlasSeries(concept, geo, signal);
  const d = r.data;
  const points = d.best
    .filter((c) => c.year !== null)
    .map((c) => ({
      year: c.year as number,
      value: c.value,
      projection: (c as { projection?: unknown }).projection === true,
      source: c.series,
      missingReason: c.value === null ? ((c.flags.missing_reason as string | undefined) ?? null) : null,
    }))
    .sort((a, b) => a.year - b.year);
  const breaks: number[] = [];
  points.forEach((p, i) => {
    if (i > 0 && p.source !== points[i - 1]!.source) breaks.push(p.year);
  });
  return {
    label: d.concept.labels.fr ?? d.concept.labels.en ?? concept,
    unit: d.best[0]?.unit ?? '',
    points,
    licences: d.licences,
    sourcesWithoutData: d.sources_without_data,
    breaks,
    snapshot: r.snapshot.snapshot_sha256,
  };
}

/** Latest observed (non-projection, non-missing) point; projections are never "the latest value". */
export function latestObserved(points: readonly SeriesPoint[]): SeriesPoint | null {
  for (let i = points.length - 1; i >= 0; i--) {
    const p = points[i]!;
    if (!p.projection && p.value !== null) return p;
  }
  return null;
}

/** A KG id the entity route can read (Wikidata Q-id or prefix:local); ISO3 countries are not KG ids. */
export function isKgId(id: string): boolean {
  return /^Q\d+$/.test(id) || /^[a-z][a-z0-9_.-]*:[^\s]+$/i.test(id);
}

export const referenceDecisions = memo(() => fetchDecisions());
