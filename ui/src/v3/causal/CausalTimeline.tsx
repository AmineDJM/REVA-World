import { formatYear } from '../lib/time';
import { buildTimeline, ticks, TEMPORAL_FR, type TimelineItem } from './model';
import type { CausalClaim, TimePoint } from './api';
import { selon } from './model';

const W = 1000;
const PAD = 70;
const LANE_Y = { cause: 34, focal: 150, effect: 196 } as const;
const ROW = 18;

function tone(claims: CausalClaim[]): string {
  if (claims.some((c) => c.temporal.status === 'inconsistent')) return 'bad';
  if (claims.some((c) => c.temporal.status === 'overlapping')) return 'warn';
  if (claims.length && claims.every((c) => c.temporal.status === 'consistent')) return 'ok';
  return 'none';
}

/**
 * Timeline alignment: causes above, the event as a band, effects below, on one year axis (astronomical years,
 * shown historically). Colour = temporal check of the claims touching each item; undated items are listed
 * apart, never placed at an invented date.
 */
export function CausalTimeline({ focal, causes, effects }: { focal: { id: string; label: string; start: TimePoint; end: TimePoint }; causes: CausalClaim[]; effects: CausalClaim[] }) {
  const m = buildTimeline(focal, causes, effects);
  const pad = Math.max(1, Math.round((m.max - m.min) * 0.06));
  const lo = m.min - pad;
  const hi = m.max + pad;
  const x = (y: number) => PAD + ((y - lo) / Math.max(1, hi - lo)) * (W - 2 * PAD);
  const lanes: Record<'cause' | 'effect', TimelineItem[]> = { cause: [], effect: [] };
  for (const it of m.items) if (it.lane !== 'focal') lanes[it.lane].push(it);
  for (const l of Object.values(lanes)) l.sort((a, b) => a.from - b.from);
  const focalItem = m.items.find((i) => i.lane === 'focal');
  const causeY = (i: number) => LANE_Y.cause + i * ROW;
  const focalY = Math.max(LANE_Y.focal, causeY(lanes.cause.length) + 30);
  const effectY = (i: number) => focalY + 46 + i * ROW;
  const svgH = Math.max(LANE_Y.effect + 40, effectY(lanes.effect.length) + 30);
  const bar = (it: TimelineItem, y: number) => {
    const x1 = x(it.from);
    const x2 = Math.max(x(it.to), x1 + 4);
    const t = tone(it.claims);
    return (
      <g key={it.key} className={`v3-ctl-item v3-tcheck-fill-${t}`} data-testid="timeline-item" data-lane={it.lane}>
        <title>{`${it.label} — ${it.dateText}${it.claims.length ? ` — ${it.claims.map((c) => `${selon(c)} ; ${TEMPORAL_FR[c.temporal.status]?.label ?? ''}`).join(' | ')}` : ''}`}</title>
        <rect x={x1} y={y - 6} width={x2 - x1} height={10} rx={3} />
        <text x={x2 > W * 0.62 ? x1 - 6 : x2 + 6} y={y + 3} className="v3-ctl-label" textAnchor={x2 > W * 0.62 ? 'end' : 'start'}>
          {it.label} ({it.dateText})
        </text>
      </g>
    );
  };
  return (
    <section className="v3-ctl" aria-labelledby="ctl-title" data-testid="causal-timeline">
      <h2 id="ctl-title">Alignement temporel</h2>
      <p className="field-hint">
        Causes au-dessus, événement au centre, conséquences en dessous. Vert : la cause précède l’effet (à la précision des dates) ; orange : chevauchement ; rouge : incohérence
        temporelle signalée (la source est montrée, pas corrigée). L’antériorité est nécessaire, jamais suffisante.
      </p>
      {m.items.length <= 1 && m.undated.length === 0 ? (
        <p className="muted">Aucune cause ou conséquence datée à aligner.</p>
      ) : (
        <svg viewBox={`0 0 ${W} ${svgH}`} className="v3-ctl-svg" role="img" aria-label={`Frise de ${focal.label} : ${lanes.cause.length} cause(s) et ${lanes.effect.length} conséquence(s) datées`}>
          {ticks(lo, hi).map((t) => (
            <g key={t} className="v3-ctl-tick">
              <line x1={x(t)} x2={x(t)} y1={14} y2={svgH - 18} />
              <text x={x(t)} y={svgH - 4} textAnchor="middle">
                {formatYear(t)}
              </text>
            </g>
          ))}
          <text x={6} y={LANE_Y.cause - 14} className="v3-ctl-lane">
            causes
          </text>
          {lanes.cause.map((it, i) => bar(it, causeY(i)))}
          {focalItem && (
            <g className="v3-ctl-focal" data-testid="timeline-focal">
              <rect x={x(focalItem.from)} y={focalY - 12} width={Math.max(x(focalItem.to) - x(focalItem.from), 6)} height={22} rx={4} />
              <text
                x={x(focalItem.from) > W * 0.55 ? Math.max(x(focalItem.to), x(focalItem.from) + 6) : x(focalItem.from)}
                y={focalY - 16}
                className="v3-ctl-label v3-ctl-focal-label"
                textAnchor={x(focalItem.from) > W * 0.55 ? 'end' : 'start'}
              >
                {focalItem.label} ({focalItem.dateText})
              </text>
            </g>
          )}
          <text x={6} y={effectY(0) - 14} className="v3-ctl-lane">
            conséquences
          </text>
          {lanes.effect.map((it, i) => bar(it, effectY(i)))}
        </svg>
      )}
      {!focalItem && <p className="v3-undated">L’événement lui-même n’est pas daté dans la source : l’alignement est indicatif.</p>}
      {m.outside.length > 0 && (
        <p className="v3-undated" data-testid="timeline-outside">
          Hors de la fenêtre affichée :{' '}
          {m.outside
            .map((u) => `${u.label} (${u.dateText}, ${u.lane === 'cause' ? 'cause' : 'conséquence'}${tone(u.claims) === 'bad' ? ', incohérence temporelle signalée' : ''})`)
            .join(' · ')}
        </p>
      )}
      {m.undated.filter((u) => u.lane !== 'focal').length > 0 && (
        <p className="v3-undated" data-testid="timeline-undated">
          Non datés dans la source :{' '}
          {m.undated
            .filter((u) => u.lane !== 'focal')
            .map((u) => `${u.label} (${u.lane === 'cause' ? 'cause' : 'conséquence'})`)
            .join(' · ')}
        </p>
      )}
    </section>
  );
}
