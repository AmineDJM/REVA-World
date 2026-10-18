/**
 * Bodies of board item cards. Each reads its live value(s) only when the card is on screen, shows the source
 * and licence of every number, and labels honesty states: projections dashed, missing hatched with its reason,
 * forecasts with their seal and judged status, decisions as "scénario non validé — pas une probabilité".
 */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import { v8Links } from '../../contract';
import { classifyV3Error } from '../../../v3/api/common';
import { getForecastQuestion, type ForecastRecord } from '../../../v3/api/forecasts';
import { getKgEntity } from '../../../v3/api/kg';
import { frDecimal, frValue } from '../../../v3/lib/format';
import { useApi } from '../../../lib/useApi';
import { Badge, MissingValue, Skeleton as ShellSkeleton, SourceChip } from '../../shell/ui';
import { isKgId, latestObserved, readSeries, referenceDecisions } from './live';
import { Markdown } from './Markdown';
import { MiniSeries } from './MiniSeries';
import { noteParts, type BoardItem } from './model';

/** True once the element has been near the viewport (IntersectionObserver; always true where unsupported). */
export function useInView<T extends Element>(): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T | null>(null);
  const [seen, setSeen] = useState(() => typeof IntersectionObserver === 'undefined');
  useEffect(() => {
    if (seen || !ref.current) return;
    const io = new IntersectionObserver(
      (es) => {
        if (es.some((e) => e.isIntersecting)) {
          setSeen(true);
          io.disconnect();
        }
      },
      { rootMargin: '200px' },
    );
    io.observe(ref.current);
    return () => io.disconnect();
  }, [seen]);
  return [ref, seen];
}

export function Missing({ reason }: { reason: string }) {
  return <MissingValue reason={reason} />;
}

function Failure({ error, what }: { error: unknown; what: string }) {
  const info = classifyV3Error(error);
  const soft = info.kind === 'unavailable' || info.kind === 'no_data' || info.kind === 'not_found';
  return (
    <p className={soft ? 'v8b-soft' : 'v8b-err'} role={soft ? 'status' : 'alert'}>
      {info.title} — {what}
      {info.status ? ` (HTTP ${info.status})` : ''}
    </p>
  );
}

/** Fixed-footprint placeholder; after 4 s it says the read is still running (some registry reads take ~10 s). */
function Skeleton() {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setSlow(true), 4000);
    return () => clearTimeout(t);
  }, []);
  return (
    <>
      <ShellSkeleton lines={3} />
      {slow && (
        <p className="v8b-meta muted" role="status">
          Lecture toujours en cours : le serveur répond lentement pour cet élément.
        </p>
      )}
    </>
  );
}

const fmtDate = (iso: string | null | undefined) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' });
};

// ------------------------------------------------------------------ series

export function SeriesBody({ item, visible }: { item: Extract<BoardItem, { kind: 'series' }>; visible: boolean }) {
  const st = useApi((s) => readSeries(item.concept, item.geo, item.headline ?? false, s), [item.concept, item.geo, item.headline], visible);
  if (st.status === 'idle' || st.status === 'loading') return <Skeleton />;
  if (st.status === 'error') return <Failure error={st.error} what={`série ${item.concept} · ${item.geo}`} />;
  const v = st.data;
  const last = latestObserved(v.points);
  const lastAny = v.points[v.points.length - 1];
  const proj = v.points.filter((p) => p.projection);
  return (
    <div className="v8b-series">
      <div className="v8b-series-head">
        {last ? (
          <span className="v8b-big v8-num" title={`Valeur exacte ${frDecimal(last.value!, 12)} · observée ${last.year} · source ${last.source}${v.snapshot ? ` · instantané ${v.snapshot.slice(0, 12)}` : ''}`}>
            {frDecimal(last.value!, headlineDigits(last.value!))}
          </span>
        ) : (
          <Missing reason={lastAny?.missingReason ?? 'aucune valeur observée sur la période chargée'} />
        )}
        <span className="v8b-unit">{v.unit}</span>
        {last && <span className="v8b-when">{last.year} · observé</span>}
      </div>
      <MiniSeries points={v.points} breaks={v.breaks} />
      <p className="v8b-meta">
        {last && (
          <SourceChip
            source={last.source}
            licence={v.licences.find((l) => last.source === l.source || last.source.startsWith(`${l.source}.`)) ?? v.licences[0] ?? null}
            period={String(last.year)}
            details={[`concept ${item.concept}`, `pays ${item.geo}`, ...(v.snapshot ? [`instantané atlas ${v.snapshot.slice(0, 16)}…`] : [])]}
          />
        )}
        {proj.length > 0 && (
          <Badge kind="projection" title="Valeurs publiées comme projections ou estimations hors période observée par la source : jamais traitées comme observations">
            projection {yearRanges(proj.map((p) => p.year))}
          </Badge>
        )}
        {v.breaks.length > 0 && <span className="v8b-badge-warn">changement de source {v.breaks.join(', ')}</span>}
      </p>
      {v.sourcesWithoutData.length > 0 && (
        <p className="v8b-meta muted v8b-wrap" title={v.sourcesWithoutData.join('\n')}>
          {v.sourcesWithoutData.length} série{v.sourcesWithoutData.length > 1 ? 's' : ''} consultée{v.sourcesWithoutData.length > 1 ? 's' : ''} sans donnée pour ce pays
        </p>
      )}
    </div>
  );
}

/** "1980–1989, 2026–2031" from sorted years. */
export function yearRanges(years: number[]): string {
  const ys = [...new Set(years)].sort((a, b) => a - b);
  const out: string[] = [];
  let a = ys[0];
  let b = ys[0];
  for (const y of ys.slice(1).concat([NaN])) {
    if (y === b! + 1) b = y;
    else {
      if (a !== undefined) out.push(a === b ? String(a) : `${a}–${b}`);
      a = b = y;
    }
  }
  return out.join(', ');
}

/** Display digits of a headline number (exact value in its tooltip): big numbers whole, small ones to 2 decimals. */
export function headlineDigits(value: string): number {
  const a = Math.abs(Number(value));
  return a >= 1000 ? 0 : a >= 100 ? 1 : 2;
}

// ------------------------------------------------------------------ entity

export function EntityBody({ item, visible }: { item: Extract<BoardItem, { kind: 'entity' }>; visible: boolean }) {
  const kg = isKgId(item.id);
  const st = useApi((s) => getKgEntity(item.id, s), [item.id], visible && kg);
  return (
    <div className="v8b-entity">
      {item.sublabel && <p className="v8b-meta">{item.sublabel}</p>}
      {!kg && (
        <p className="v8b-meta">
          <code>{item.id}</code> · {item.entityKind}
        </p>
      )}
      {kg && (st.status === 'loading' || st.status === 'idle') && <Skeleton />}
      {kg && st.status === 'error' && <Failure error={st.error} what={`entité ${item.id}`} />}
      {kg && st.status === 'ok' && (
        <>
          <p className="v8b-desc">{st.data.data.descriptions.fr || st.data.data.descriptions.en || <span className="muted">Pas de description dans la source.</span>}</p>
          <p className="v8b-meta">
            <code>{st.data.data.qid}</code> · {st.data.data.statements_out} déclarations sortantes · {st.data.data.statements_in} entrantes
            {st.data.data.provenance && (
              <>
                {' '}
                · source <code>{st.data.data.provenance.source_key}</code>
              </>
            )}
            {st.data.data.provenance?.source_key.startsWith('wikidata') && <Badge kind="community" />}
          </p>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ forecast

function str(x: unknown): string | null {
  return typeof x === 'string' ? x : typeof x === 'number' ? String(x) : null;
}

const MODEL_FR: Record<string, string> = {
  'forecast.registry_persistence': 'Persistance',
  'forecast.registry_seasonal_naive': 'Saisonnier naïf',
  'forecast.registry_ar': 'AR(p)',
  'forecast.registry_hist_quantile': 'Quantiles historiques',
  'forecast.reference_imf_weo': 'FMI — WEO',
  'reva.ensemble': 'Combinaison REVA',
};

export function modelLabel(f: Pick<ForecastRecord, 'model'> & { model_version?: unknown }): string {
  const base = MODEL_FR[f.model] ?? f.model.replace(/^forecast\./, '');
  return typeof f.model_version === 'string' ? `${base} v${f.model_version}` : base;
}

function ForecastValue({ f, unit }: { f: ForecastRecord; unit: string }) {
  if (f.status === 'abstained') return <span className="muted">abstention{f.reason ? ` (${f.reason})` : ''}</span>;
  const parts: ReactNode[] = [];
  if (f.point) parts.push(<span key="p" className="v8-num">{frValue(f.point, unit)}</span>);
  const qs = (f as { quantiles?: Record<string, string> }).quantiles;
  const iv = f.interval && f.interval.length === 2 ? f.interval : qs && qs['0.05'] && qs['0.95'] ? [qs['0.05'], qs['0.95']] : null;
  if (iv)
    parts.push(
      <span key="i" className="v8-num muted" title={f.interval ? 'Intervalle publié' : 'Quantiles 5 % – 95 % de la distribution scellée'}>
        [{frValue(iv[0]!, unit)} ; {frValue(iv[1]!, unit)}]
      </span>,
    );
  if (f.prob_above) parts.push(<span key="b" className="v8-num">P = {frDecimal(f.prob_above, 2)}</span>);
  return parts.length ? <>{parts.reduce<ReactNode[]>((a, p, i) => (i ? [...a, ' ', p] : [p]), [])}</> : <span className="muted">—</span>;
}

const STATUS_FR: Record<string, string> = { pending: 'en attente', resolved: 'résolue', void: 'annulée', not_prospective: 'non prospective' };

export function ForecastBody({ item, visible, compact = false }: { item: Extract<BoardItem, { kind: 'forecast' }>; visible: boolean; compact?: boolean }) {
  const st = useApi((s) => getForecastQuestion(item.questionId, s), [item.questionId], visible);
  if (st.status === 'idle' || st.status === 'loading') return <Skeleton />;
  if (st.status === 'error') return <Failure error={st.error} what={`question ${item.questionId}`} />;
  const d = st.data.data;
  const q = d.question;
  const unit = str(q.unit) ?? '';
  const res = (q.resolution ?? {}) as Record<string, unknown>;
  const lastObs = (q.last_observed ?? null) as { period?: string; value?: string } | null;
  const outcome = d.outcome as Record<string, unknown> | null;
  const issued = d.forecasts.filter((f) => f.status === 'issued');
  const official = issued.filter((f) => f.attribution);
  const shown = compact ? [...official, ...issued.filter((f) => !f.attribution)].slice(0, 5) : d.forecasts;
  return (
    <div className="v8b-forecast">
      <p className="v8b-qtext">{str(q.text_fr) ?? item.label}</p>
      <p className="v8b-meta">
        {d.status === 'resolved' ? <Badge kind="judged">résolue · jugée</Badge> : d.status === 'void' ? <Badge kind="missing">annulée</Badge> : <Badge kind="info">{STATUS_FR[d.status] ?? d.status}</Badge>}
        {d.status === 'pending' && <span>résolution prévue le {fmtDate(str(res.expected_date))}</span>}
        {d.status === 'resolved' && outcome && (
          <span>
            observé : <strong className="v8-num">{str(outcome.value) ? frValue(str(outcome.value)!, unit) : '—'}</strong>
          </span>
        )}
        {lastObs?.value && (
          <span className="muted">
            dernier publié : {frValue(lastObs.value, unit)} ({lastObs.period})
          </span>
        )}
      </p>
      <p className="v8b-meta">
        {d.seal.verified ? (
          <span className="v8b-seal-ok" title={`Lot ${d.seal.batch_sha256} : empreinte re-vérifiée à la lecture`}>
            ✓ scellée le {fmtDate(d.seal.issued_at)} · sceau vérifié
          </span>
        ) : (
          <span className="v8b-seal-bad">sceau NON vérifié — ne pas utiliser</span>
        )}
      </p>
      <table className="v8b-fc">
        <caption className="sr-only">Prévisions scellées</caption>
        <thead>
          <tr>
            <th scope="col">Prévisionniste</th>
            <th scope="col">Prévision</th>
            {d.status === 'resolved' && <th scope="col">Score</th>}
          </tr>
        </thead>
        <tbody>
          {shown.map((f, i) => (
            <tr key={`${f.model}-${i}`} className={f.attribution ? 'v8b-fc-official' : undefined}>
              <th scope="row" title={f.attribution ? `${f.model} — ${f.attribution}` : f.model}>
                {modelLabel(f)}
                {f.attribution && (
                  <>
                    {' '}
                    <Badge kind="info" size="xs" title={f.attribution}>
                      officiel · attribué
                    </Badge>
                  </>
                )}
              </th>
              <td>
                <ForecastValue f={f} unit={unit} />
              </td>
              {d.status === 'resolved' && (
                <td className="v8-num">
                  {f.score
                    ? Object.entries(f.score)
                        .filter(([, v]) => typeof v === 'number')
                        .slice(0, 2)
                        .map(([k, v]) => `${k} ${frDecimal(String(v), 3)}`)
                        .join(' · ')
                    : '—'}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {compact && issued.length > shown.length && <p className="v8b-meta muted">+ {issued.length - shown.length} autres prévisions</p>}
      <p className="v8b-meta muted">
        {d.status === 'resolved'
          ? 'Une seule question ne fait pas un verdict : le jugement contre le prévisionniste officiel est sur le tableau des scores.'
          : 'Prévisions de référence (lignes de base) scellées avant la résolution ; pas un conseil.'}{' '}
        <Link to={v8Links.scoreboard(str(q.family) ?? undefined)}>Scores</Link>
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ decision

export function DecisionBody({ item, visible }: { item: Extract<BoardItem, { kind: 'decision' }>; visible: boolean }) {
  const st = useApi(() => referenceDecisions(), [], visible);
  const ref = st.status === 'ok' ? st.data.decisions.find((d) => d.id === item.ref) : undefined;
  return (
    <div className="v8b-decision">
      <p className="v8b-meta">
        <Badge kind="scenario">scénario non validé — pas une probabilité</Badge>
      </p>
      {item.summary && <p className="v8b-desc">{item.summary}</p>}
      {ref && (
        <dl className="v8b-dl">
          <dt>Acteur</dt>
          <dd>{ref.actor.label_fr}</dd>
          <dt>Instrument</dt>
          <dd>
            <code>{ref.instrument}</code> · {frDecimal(ref.magnitude.value)} {ref.magnitude.unit}
          </dd>
          <dt>Début</dt>
          <dd>
            {ref.start} · horizon {ref.horizon_quarters} trimestres
          </dd>
          <dt>Pays suivis</dt>
          <dd>{ref.countries.join(', ')}</dd>
        </dl>
      )}
      {st.status === 'ok' && !ref && !item.summary && <p className="v8b-meta muted">Décision composée : résumé non fourni à l’épinglage.</p>}
      {st.status === 'error' && <Failure error={st.error} what="décisions de référence" />}
      <p className="v8b-meta">
        <Link to={v8Links.decision(item.ref)}>Ouvrir dans Décision</Link> — la propagation (effets attribués, éventails) se calcule là-bas.
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ note

export function NoteBody({ item, editable, onSave }: { item: Extract<BoardItem, { kind: 'note' }>; editable: boolean; onSave: (md: string) => void }) {
  const [editing, setEditing] = useState(item.markdown === '' && editable);
  const [draft, setDraft] = useState(item.markdown);
  const ta = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (editing) ta.current?.focus();
  }, [editing]);
  if (editing)
    return (
      <form
        className="v8b-note-edit"
        onSubmit={(e) => {
          e.preventDefault();
          onSave(draft);
          setEditing(false);
        }}
      >
        <label className="sr-only" htmlFor={`md-${item.uid}`}>
          Texte de la note (Markdown)
        </label>
        <textarea
          id={`md-${item.uid}`}
          ref={ta}
          value={draft}
          rows={6}
          maxLength={20000}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              onSave(draft);
              setEditing(false);
            } else if (e.key === 'Escape') {
              e.stopPropagation();
              setDraft(item.markdown);
              setEditing(false);
            }
          }}
          placeholder="# Titre&#10;- un point&#10;**gras**, *italique*, [lien](/v8/…)"
        />
        <div className="v8b-row">
          <button type="submit" className="v8-btn v8-btn-primary">
            Enregistrer <kbd>Ctrl</kbd>+<kbd>Entrée</kbd>
          </button>
          <button
            type="button"
            className="v8-btn"
            onClick={() => {
              setDraft(item.markdown);
              setEditing(false);
            }}
          >
            Annuler
          </button>
        </div>
      </form>
    );
  return (
    <div className="v8b-note">
      {(() => {
        const { body } = noteParts(item);
        return body.trim() ? <Markdown source={body} /> : item.markdown.trim() ? null : <p className="muted">Note vide.</p>;
      })()}
      {editable && (
        <button type="button" className="v8-btn v8-btn-ghost v8-btn-sm" onClick={() => setEditing(true)}>
          Modifier la note
        </button>
      )}
    </div>
  );
}
