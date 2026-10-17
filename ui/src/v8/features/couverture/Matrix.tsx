import { useMemo, useRef, type KeyboardEvent } from 'react';
import type { Column, Matrix as MatrixData, Row } from './api';
import { cellText, cellTitle, hasRows, intensity, type Level, type Metric } from './model';

/**
 * Domain × place heat matrix. Rows = domains (click → domain detail), columns = continents, sub-regions or countries
 * (country header click → country profile). Arrow keys move between cells, Enter opens, Home/End jump in a row.
 * A cell with nothing stored is hatched (no value colour); a cell with only KG entities is dotted.
 */
export function HeatMatrix({
  data,
  metric,
  onDomain,
  onCell,
  onCountry,
  selectedDomain,
  selectedCol,
}: {
  data: MatrixData;
  metric: Metric;
  onDomain: (id: string) => void;
  onCell: (row: Row, col: Column) => void;
  onCountry: (iso3: string) => void;
  selectedDomain: string;
  selectedCol: string;
}) {
  const level = data.level as Level;
  const thisYear = Number(data.meta.today.slice(0, 4)) || new Date().getFullYear();
  const maxRows = useMemo(() => {
    let m = 0;
    for (const r of data.rows) for (const c of Object.values(r.cells)) m = Math.max(m, c.rows ?? 0);
    return m;
  }, [data]);
  const groups = useMemo(() => {
    if (level === 'region') return null;
    const out: { region: string; label: string; span: number }[] = [];
    for (const c of data.columns) {
      const last = out[out.length - 1];
      if (last && last.region === c.region) last.span += 1;
      else out.push({ region: c.region, label: regionLabel(c.region), span: 1 });
    }
    return out;
  }, [data, level]);
  const ref = useRef<HTMLTableElement>(null);

  const onKey = (e: KeyboardEvent<HTMLTableElement>) => {
    const t = e.target as HTMLElement;
    const r = Number(t.dataset.r);
    const c = Number(t.dataset.c);
    if (!Number.isFinite(r) || !Number.isFinite(c)) return;
    let nr = r;
    let nc = c;
    if (e.key === 'ArrowRight') nc = c + 1;
    else if (e.key === 'ArrowLeft') nc = c - 1;
    else if (e.key === 'ArrowDown') nr = r + 1;
    else if (e.key === 'ArrowUp') nr = r - 1;
    else if (e.key === 'Home') nc = -1;
    else if (e.key === 'End') nc = data.columns.length - 1;
    else return;
    e.preventDefault();
    nr = Math.max(0, Math.min(data.rows.length - 1, nr));
    nc = Math.max(-1, Math.min(data.columns.length - 1, nc));
    const next = ref.current?.querySelector<HTMLElement>(`[data-r="${nr}"][data-c="${nc}"]`);
    next?.focus();
  };

  return (
    <div className={`cv-matrix-wrap cv-level-${level}`}>
      <table className="cv-matrix" ref={ref} onKeyDown={onKey} aria-label="Couverture par domaine et par lieu">
        <thead>
          {groups && (
            <tr className="cv-groups">
              <th className="cv-corner" aria-hidden="true" />
              {groups.map((g) => (
                <th key={g.region} colSpan={g.span} scope="colgroup" className="cv-group">
                  <span>{g.label}</span>
                </th>
              ))}
            </tr>
          )}
          <tr>
            <th className="cv-corner" scope="col">
              Domaine
            </th>
            {data.columns.map((col) => (
              <th key={col.id} scope="col" className={`cv-col${col.id === selectedCol ? ' is-sel' : ''}`} title={colTitle(col, level)}>
                {level === 'country' ? (
                  <button type="button" className="cv-colbtn" onClick={() => onCountry(col.id)} aria-label={`Profil de couverture : ${col.label}`} tabIndex={-1}>
                    {col.id}
                    {col.placement === 'reva' && <sup aria-hidden="true">*</sup>}
                  </button>
                ) : (
                  <span>
                    {col.label}
                    <small className="cv-colcount v8-num">{col.countries}</small>
                  </span>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row, ri) => (
            <tr key={row.id} className={row.id === selectedDomain ? 'is-sel' : undefined}>
              <th scope="row" className="cv-rowhead">
                <button type="button" data-r={ri} data-c={-1} tabIndex={ri === 0 ? 0 : -1} onClick={() => onDomain(row.id)} className="cv-rowbtn" title={`${row.label} — détail du domaine`}>
                  <span className="cv-rowlabel">{row.label}</span>
                  <span className="cv-rowmeta v8-num">
                    {row.summary.tables > 0 ? `${row.summary.tables} t · ${shortRows(row.summary.rows)}` : 'aucune table'}
                    {row.summary.kg_entities > 0 ? ` · KG ${shortRows(row.summary.kg_entities)}` : ''}
                  </span>
                </button>
              </th>
              {data.columns.map((col, ci) => {
                const cell = row.cells[col.id];
                const k = intensity(cell, col, metric, maxRows, thisYear);
                const title = cellTitle(row.label, col, cell, level);
                const cls = !cell ? 'is-none' : !hasRows(cell) ? 'is-kg' : 'is-data';
                return (
                  <td key={col.id} className={`cv-cell ${cls}`}>
                    <button
                      type="button"
                      data-r={ri}
                      data-c={ci}
                      tabIndex={-1}
                      title={title}
                      aria-label={title}
                      className={`cv-cellbtn${k !== null && k >= 0.62 && hasRows(cell) ? ' is-hi' : ''}`}
                      style={k !== null && hasRows(cell) ? { ['--k' as string]: String(Math.round(k * 100)) } : undefined}
                      onClick={() => onCell(row, col)}
                    >
                      <span className="v8-num">{cellText(cell, col, metric, level)}</span>
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const REGION_FR: Record<string, string> = { Africa: 'Afrique', Americas: 'Amériques', Asia: 'Asie', Europe: 'Europe', Oceania: 'Océanie', Antarctica: 'Antarctique' };
function regionLabel(r: string): string {
  return REGION_FR[r] ?? r;
}

function colTitle(col: Column, level: Level): string {
  if (level === 'country') {
    const base = `${col.label} (${col.id})`;
    return col.placement === 'reva' ? `${base} — placement REVA hors M49 : ${col.placement_basis ?? ''}` : base;
  }
  return `${col.label} : ${col.countries} pays ou territoires`;
}

function shortRows(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toLocaleString('fr-FR', { maximumFractionDigits: 1 })} M`;
  if (n >= 1e3) return `${(n / 1e3).toLocaleString('fr-FR', { maximumFractionDigits: 0 })} k`;
  return String(n);
}

export function MatrixLegend({ metric }: { metric: Metric }) {
  const what = metric === 'pays' ? 'part des pays avec des données' : metric === 'lignes' ? 'lignes stockées (log)' : 'fraîcheur de la dernière année observée';
  return (
    <div className="cv-legend" aria-label="Légende">
      <span className="cv-legend-ramp" aria-hidden="true" />
      <span className="v8-muted">faible → fort : {what}</span>
      <span className="cv-legend-sw is-kg" aria-hidden="true" />
      <span className="v8-muted">entités KG seulement</span>
      <span className="cv-legend-sw is-none" aria-hidden="true" />
      <span className="v8-muted">rien de rattaché à ce lieu (≠ absence du phénomène)</span>
    </div>
  );
}
