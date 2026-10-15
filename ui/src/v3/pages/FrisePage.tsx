import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type WheelEvent } from 'react';
import { Link, useSearchParams } from 'react-router';
import { useVirtualizer } from '@tanstack/react-virtual';
import { entityLabel, getKgEntity, getKgTimeline } from '../api/kg';
import { useApi } from '../../lib/useApi';
import { CitationChip } from '../components/CitationChip';
import { EntityPicker, type PickedEntity } from '../components/EntityPicker';
import { V3Empty, V3Error, V3Loading } from '../components/States';
import { citeKgStatement } from '../lib/citations';
import { propertyLabel } from '../lib/graph';
import { formatYear } from '../lib/time';
import { buildTimeline, packRows, tickStep, ticks, zoomWindow, type TimelineItem, type TimelineModel } from '../lib/timeline';

const LANE_COLORS = ['#3f84cc', '#e07b39', '#4caf7a', '#b65fc5', '#d4b13a', '#d9534f', '#2bb3b1', '#8d6e63', '#7986cb', '#a0a83a'];

export default function FrisePage() {
  const [params, setParams] = useSearchParams();
  const qid = params.get('qid');
  const ent = useApi((s) => getKgEntity(qid!, s), [qid], !!qid);
  const tl = useApi((s) => getKgTimeline(qid!, { include_incoming: true, limit: 1000 }, s), [qid], !!qid);
  const label = ent.status === 'ok' ? entityLabel(ent.data.data, qid ?? '') : (tl.data?.data.label ?? qid ?? '');
  const model = useMemo(() => (tl.status === 'ok' && qid ? buildTimeline(qid, tl.data.data.events) : null), [tl.status, tl.data, qid]);

  const onPick = (e: PickedEntity | null) => {
    const p = new URLSearchParams(params);
    if (e) p.set('qid', e.qid);
    else p.delete('qid');
    setParams(p);
  };

  return (
    <section className="v3-page v3-frise" aria-labelledby="frise-title">
      <header className="v3-page-head">
        <h1 id="frise-title">Frise{qid ? ` — ${label}` : ''}</h1>
        <p className="v3-lede">Événements, appartenances et fonctions datés d’une entité, des siècles aux années. Chaque élément cite sa déclaration.</p>
      </header>
      <div className="v3-toolbar">
        <EntityPicker label="Entité" value={qid ? { qid, label } : null} onChange={onPick} testId="frise-picker" />
        {qid && (
          <p className="v3-entity-actions">
            <Link to={`/v3/neurones?qid=${encodeURIComponent(qid)}`}>Neurones</Link>
            <Link to={`/v3/chemin?from=${encodeURIComponent(qid)}`}>Chemin</Link>
          </p>
        )}
      </div>
      {!qid && (
        <V3Empty>
          <p>
            <strong>Choisissez une entité</strong> (pays, empire, personne…) pour afficher sa frise.
          </p>
        </V3Empty>
      )}
      {qid && tl.status === 'loading' && <V3Loading what="de la frise" />}
      {qid && tl.status === 'error' && <V3Error error={tl.error} what="La frise" onRetry={tl.reload} />}
      {model && tl.status === 'ok' && (
        <>
          {tl.data.data.truncated && <p className="v3-notice">Frise limitée aux 1000 premiers éléments par le serveur.</p>}
          {model.items.length === 0 ? (
            <V3Empty>
              <p>
                <strong>Aucun élément daté</strong> pour cette entité dans les données chargées{model.undated.length ? ` (${model.undated.length} déclaration(s) non datée(s) listée(s) ci-dessous)` : ''}.
              </p>
            </V3Empty>
          ) : (
            <TimelineView model={model} />
          )}
          <EventTable model={model} />
        </>
      )}
    </section>
  );
}

function TimelineView({ model }: { model: TimelineModel }) {
  const pad = Math.max(1, Math.round((model.max - model.min) * 0.03));
  const bounds: [number, number] = [model.min - pad, model.max + pad];
  const [win, setWin] = useState<[number, number]>(bounds);
  const [hover, setHover] = useState<TimelineItem | null>(null);
  const [selected, setSelected] = useState<TimelineItem | null>(null);
  const svgWrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);
  const drag = useRef<{ x: number; win: [number, number] } | null>(null);

  useEffect(() => setWin([model.min - pad, model.max + pad]), [model, pad]);
  useEffect(() => {
    const el = svgWrap.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((e) => setWidth(Math.max(320, e[0]!.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const left = 150;
  const plotW = width - left - 16;
  const x = useCallback((y: number) => left + ((y - win[0]) / Math.max(1e-9, win[1] - win[0])) * plotW, [win, plotW]);
  const rowH = 18;
  const layout = useMemo(() => {
    let yCursor = 34;
    const yearsPerPx = (win[1] - win[0]) / Math.max(1, plotW);
    return model.lanes.map((lane, li) => {
      const items = model.items.filter((i) => i.lane === lane);
      const rows = packRows(items, yearsPerPx * 6);
      const nRows = Math.max(1, ...rows.map((r) => r + 1));
      const top = yCursor;
      yCursor += nRows * rowH + 14;
      return { lane, color: LANE_COLORS[li % LANE_COLORS.length]!, items, rows, top, height: nRows * rowH };
    });
  }, [model, win, plotW]);
  const height = (layout.length ? layout[layout.length - 1]!.top + layout[layout.length - 1]!.height : 40) + 24;
  const span = win[1] - win[0];

  const zoom = (factor: number, center = (win[0] + win[1]) / 2) => setWin(zoomWindow(win[0], win[1], factor, center, bounds[0], bounds[1]));
  const pan = (fraction: number) => {
    const d = span * fraction;
    let a = win[0] + d;
    let b = win[1] + d;
    if (a < bounds[0]) {
      b += bounds[0] - a;
      a = bounds[0];
    }
    if (b > bounds[1]) {
      a -= b - bounds[1];
      b = bounds[1];
    }
    setWin([a, b]);
  };
  const onWheel = (e: WheelEvent<SVGSVGElement>) => {
    if (!e.ctrlKey && !e.metaKey && Math.abs(e.deltaY) < Math.abs(e.deltaX)) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const yr = win[0] + ((e.clientX - rect.left - left) / plotW) * span;
    zoom(e.deltaY > 0 ? 1.25 : 0.8, yr);
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === '+' || e.key === '=') zoom(0.5);
    else if (e.key === '-') zoom(2);
    else if (e.key === 'ArrowLeft') pan(-0.2);
    else if (e.key === 'ArrowRight') pan(0.2);
    else if (e.key === '0') setWin(bounds);
    else return;
    e.preventDefault();
  };
  const onDown = (e: PointerEvent<SVGSVGElement>) => {
    drag.current = { x: e.clientX, win };
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
  };
  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x;
    const dYears = -(dx / plotW) * (drag.current.win[1] - drag.current.win[0]);
    let a = drag.current.win[0] + dYears;
    let b = drag.current.win[1] + dYears;
    if (a < bounds[0]) {
      b += bounds[0] - a;
      a = bounds[0];
    }
    if (b > bounds[1]) {
      a -= b - bounds[1];
      b = bounds[1];
    }
    setWin([a, b]);
  };
  const shown = hover ?? selected;
  const step = tickStep(span);

  return (
    <div className="v3-timeline">
      <div className="v3-timeline-controls" role="toolbar" aria-label="Zoom de la frise">
        <button type="button" onClick={() => zoom(0.5)} aria-label="Zoomer">
          +
        </button>
        <button type="button" onClick={() => zoom(2)} aria-label="Dézoomer">
          −
        </button>
        <button type="button" onClick={() => pan(-0.25)} aria-label="Plus tôt">
          ←
        </button>
        <button type="button" onClick={() => pan(0.25)} aria-label="Plus tard">
          →
        </button>
        <button type="button" onClick={() => setWin(bounds)}>
          Tout
        </button>
        <span className="muted" aria-live="polite">
          {formatYear(Math.round(win[0]))} – {formatYear(Math.round(win[1]))} · graduation {step >= 100 ? `${step / 100} siècle${step >= 200 ? 's' : ''}` : `${step} an${step > 1 ? 's' : ''}`}
        </span>
        <span className="field-hint">Molette + Ctrl ou boutons pour zoomer ; glisser pour se déplacer ; touches + − ← → 0.</span>
      </div>
      <div ref={svgWrap} className="v3-timeline-wrap" tabIndex={0} onKeyDown={onKey} aria-label="Frise chronologique (le tableau ci-dessous en donne le contenu)">
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={`Frise de ${model.items.length} éléments entre ${formatYear(model.min)} et ${formatYear(model.max)}`}
          onWheel={onWheel}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={() => (drag.current = null)}
          onPointerLeave={() => (drag.current = null)}
          data-testid="timeline-svg"
        >
          <defs>
            <linearGradient id="v3-fade-r" x1="0" x2="1">
              <stop offset="0" stopOpacity="1" stopColor="currentColor" />
              <stop offset="1" stopOpacity="0" stopColor="currentColor" />
            </linearGradient>
            <clipPath id="v3-plot">
              <rect x={left} y={0} width={plotW} height={height} />
            </clipPath>
          </defs>
          {ticks(win[0], win[1]).map((t) => (
            <g key={t}>
              <line x1={x(t)} x2={x(t)} y1={22} y2={height - 6} className="v3-tick" />
              <text x={x(t)} y={14} textAnchor="middle" className="v3-tick-label">
                {formatYear(t)}
              </text>
            </g>
          ))}
          {layout.map((l) => (
            <g key={l.lane}>
              <text x={8} y={l.top + 13} className="v3-lane-label">
                {l.lane.length > 22 ? `${l.lane.slice(0, 21)}…` : l.lane}
                <title>{l.lane}</title>
              </text>
              <g clipPath="url(#v3-plot)">
                {l.items.map((it, i) => {
                  const y = l.top + l.rows[i]! * rowH;
                  const isPoint = it.start === it.end && it.temporal.kind !== 'interval';
                  const active = shown?.id === it.id;
                  const common = {
                    onMouseEnter: () => setHover(it),
                    onMouseLeave: () => setHover(null),
                    onClick: () => setSelected(it),
                    className: `v3-tl-item${active ? ' active' : ''}${it.precision !== null && it.precision < 9 ? ' coarse' : ''}`,
                  };
                  if (isPoint) {
                    return (
                      <g key={it.id} {...common}>
                        <circle cx={x(it.start + 0.5)} cy={y + 8} r={5} fill={l.color} />
                        <title>{`${it.title} — ${it.dates}`}</title>
                      </g>
                    );
                  }
                  const x0 = x(it.start);
                  const x1 = Math.max(x0 + 3, x(it.end + 1));
                  return (
                    <g key={it.id} {...common}>
                      <rect x={x0} y={y + 2} width={x1 - x0} height={12} rx={3} fill={l.color} opacity={it.openEnd || it.openStart ? 0.55 : 0.9} />
                      {it.openEnd && <rect x={x1 - 18} y={y + 2} width={18} height={12} fill="url(#v3-fade-r)" style={{ color: 'var(--v3-bg, #fff)' }} />}
                      {x1 - x0 > 60 && (
                        <text x={x0 + 4} y={y + 12} className="v3-tl-text">
                          {(it.other?.label ?? it.title).slice(0, Math.floor((x1 - x0) / 6.5))}
                        </text>
                      )}
                      <title>{`${it.title} — ${it.dates}${it.openEnd ? ' (fin non précisée par la source)' : ''}`}</title>
                    </g>
                  );
                })}
              </g>
            </g>
          ))}
        </svg>
      </div>
      <div className="v3-tl-detail" aria-live="polite" data-testid="timeline-detail">
        {shown ? (
          <>
            <p>
              <strong>{shown.title}</strong> — {shown.dates}
              {shown.openEnd && ' (fin non précisée par la source)'}
              {shown.openStart && ' (début non précisé par la source)'}
            </p>
            <CitationChip c={shown.citation} />
          </>
        ) : (
          <p className="muted">Survolez ou cliquez un élément pour voir sa citation.</p>
        )}
      </div>
    </div>
  );
}

function EventTable({ model }: { model: TimelineModel }) {
  const parent = useRef<HTMLDivElement>(null);
  const rows = model.items;
  const v = useVirtualizer({ count: rows.length, getScrollElement: () => parent.current, estimateSize: () => 64, overscan: 8 });
  const vi = v.getVirtualItems();
  const list = vi.length ? vi.map((r) => ({ i: r.index, start: r.start, key: r.key })) : rows.slice(0, 50).map((_, i) => ({ i, start: -1, key: i }));
  return (
    <section aria-labelledby="frise-table" className="v3-tl-table">
      <h2 id="frise-table">
        Éléments datés ({rows.length}){model.undated.length ? ` · non datés : ${model.undated.length}` : ''}
      </h2>
      <div ref={parent} className="v3-virtual-list" role="list">
        <div style={{ height: vi.length ? v.getTotalSize() : undefined, position: 'relative' }}>
          {list.map((r) => {
            const it = rows[r.i]!;
            return (
              <div
                key={r.key}
                role="listitem"
                data-index={r.i}
                ref={vi.length ? v.measureElement : undefined}
                className="v3-tl-row"
                style={r.start >= 0 ? { position: 'absolute', top: 0, left: 0, right: 0, transform: `translateY(${r.start}px)` } : undefined}
              >
                <span className="v3-tl-dates">{it.dates}</span>
                <span className="v3-tl-title">
                  {it.other ? (
                    <>
                      {it.lane} : <Link to={`/v3/frise?qid=${encodeURIComponent(it.other.qid)}`}>{it.other.label}</Link>
                    </>
                  ) : (
                    it.title
                  )}
                </span>
                <CitationChip c={it.citation} compact />
              </div>
            );
          })}
        </div>
      </div>
      {model.undated.length > 0 && (
        <details className="v3-undated-list">
          <summary>Déclarations non datées dans la source ({model.undated.length}) — non placées sur la frise</summary>
          <ul>
            {model.undated.slice(0, 200).map((ev) => (
              <li key={`${ev.provenance.statement_key}-${ev.row_id}`}>
                {propertyLabel(ev.property)} : {ev.object?.label ?? ev.subject.label} <CitationChip c={citeKgStatement(ev)} compact />
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
