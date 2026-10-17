/**
 * CSV export of exactly what Comparer shows: one row per (indicator, place, period) of the current view, the
 * shown value and unit (after the transform), the reported value it comes from, and the source columns
 * (series, licence, raw artifact, projection, missing reason, population input of a per-capita computation).
 */
import type { MultiResponse, MultiSeries, Rights } from '../monde/api';
import type { Shown, TransformId } from './transforms';

export interface CsvPanel {
  series: MultiSeries;
  transform: TransformId;
  unit: string;
  shown: Record<string, Shown[]>;
  licences: Rights[];
  vintage: string | null;
}

export const CSV_COLUMNS = [
  'indicator',
  'series',
  'place_iso3',
  'place_name',
  'period',
  'period_start',
  'period_end',
  'shown_value',
  'shown_unit',
  'transform',
  'reported_value',
  'reported_unit',
  'projection',
  'missing_reason',
  'source',
  'licence',
  'non_commercial',
  'raw_artifact_sha256',
  'vintage',
  'computation',
  'population_value',
  'population_series',
];

export function csvCell(v: unknown): string {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n\r;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function buildCsv(panels: CsvPanel[], geos: string[], nameOf: (g: string) => string, population: MultiResponse['data']['population']): string {
  const lines = [CSV_COLUMNS.join(',')];
  for (const pnl of panels) {
    const m = pnl.series.meta;
    const label = m.label ?? m.concept?.labels.fr ?? m.measure;
    for (const g of geos) {
      for (const x of pnl.shown[g] ?? []) {
        const src = x.src;
        const lic = src && src.lic !== null ? pnl.licences[src.lic] : (m.licences[0] ?? null);
        const computation =
          pnl.transform === 'percap'
            ? `REVA: ${m.series} / population (${population?.concept?.id ?? 'atlas'})`
            : pnl.transform === 'index'
              ? `REVA: base 100 (${x.note ?? ''})`
              : pnl.transform === 'yoy'
                ? `REVA: change over one year % (${x.note ?? ''})`
                : '';
        lines.push(
          [
            label,
            m.series,
            g,
            nameOf(g),
            x.p,
            src?.ps ?? '',
            src?.pe ?? '',
            x.y === null ? '' : String(x.y),
            pnl.unit,
            pnl.transform,
            src?.v ?? src?.t ?? '',
            src?.unit ?? m.unit ?? '',
            x.proj ? 'true' : 'false',
            x.y === null ? (x.miss ?? 'missing') : '',
            src?.series ?? m.series,
            lic?.licence ?? '',
            lic?.non_commercial ? 'true' : 'false',
            src?.raw ?? '',
            pnl.vintage ?? '',
            computation,
            x.pop?.v ?? '',
            x.pop?.series ?? '',
          ]
            .map(csvCell)
            .join(','),
        );
      }
    }
  }
  return lines.join('\r\n') + '\r\n';
}

export function downloadCsv(text: string, filename: string): void {
  const blob = new Blob(['﻿', text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
