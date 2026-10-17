/**
 * Scores — the honest scoreboard of REVA's numeric forecasts against every official forecaster (owner V8SCORES).
 *
 * Answer first: how many judged comparisons REVA wins, loses, ties or awaits against official forecasters, and
 * which ones it wins (each with its caveats). Then the matrix (targets × forecasters), the calendar of pending
 * resolutions (pre-registered tests + live registry), the live registry counts, the tracks, and how REVA is judged.
 * URL state: ?track=<id> (track detail), ?cell=<target~forecaster> (cell drawer), ?doc=<path> (report reader),
 * ?f=<forecaster> (one column), ?d=<domain> (one domain), ?section=calendrier|methode (scroll).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { v8Links } from '../../contract';
import { Badge, EmptyState, ErrorState, KeyHint, Panel, Skeleton, Stat, useUrlParam } from '../../shell/ui';
import { getCell, getLive, getScores, getTrack, type CalendarItem, type CellDetail, type Counts, type LiveState, type ScoresPage as Page, type TrackDetail, type Vocab, type Win } from './api';
import { ChartView } from './Charts';
import { DocViewer } from './DocViewer';
import { Matrix } from './Matrix';
import { RowsTable } from './RowsTable';
import { EVALUATION_SHORT, METRIC_SHORT, VERDICT_FR, dayLabel, daysUntil, fmtP, fmtRatio, forecasterLabel, total, whenLabel } from './model';
import './scores.css';

type Load<T> = { status: 'loading' } | { status: 'ok'; data: T } | { status: 'error'; error: unknown };

function useLoad<T>(fn: ((signal: AbortSignal) => Promise<T>) | null, deps: unknown[]): [Load<T>, () => void] {
  const [state, setState] = useState<Load<T>>({ status: 'loading' });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!fn) return;
    const ac = new AbortController();
    setState({ status: 'loading' });
    fn(ac.signal)
      .then((data) => setState({ status: 'ok', data }))
      .catch((error: unknown) => !ac.signal.aborted && setState({ status: 'error', error }));
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return [state, () => setTick((t) => t + 1)];
}

// ------------------------------------------------------------------------------------------------ headline
function CountsBar({ c, label }: { c: Counts; label: string }) {
  const n = total(c);
  if (n === 0) return null;
  return (
    <div className="sc-bar" role="img" aria-label={label}>
      {(['bat', 'egalite', 'ne_bat_pas', 'en_attente'] as const).map((k) =>
        c[k] ? (
          <span key={k} className={`sc-bar-seg sc-seg-${k}`} style={{ flexGrow: c[k] }} title={`${c[k]} ${VERDICT_FR[k]}`}>
            {c[k] / n > 0.06 ? c[k] : ''}
          </span>
        ) : null,
      )}
    </div>
  );
}

function WinLine({ w, vocab, onOpenTrack }: { w: Win; vocab: Vocab; onOpenTrack: (id: string, row?: string) => void }) {
  const key = w.caveats.filter((c) => ['timeliness_edge', 'uses_official_forecast', 'final_vintage_outcome', 'same_institution_outcome', 'licence_restricted'].includes(c));
  return (
    <li className="sc-win">
      <button type="button" className="sc-win-btn" onClick={() => onOpenTrack(w.track, w.id)}>
        <span className="sc-win-ratio v8-num">{fmtRatio(w.ratio)}</span>
        <span className="sc-win-text">
          <strong>{w.candidate_label_fr}</strong> bat <em>{w.benchmark_label_fr}</em> — {forecasterLabel(w.forecaster)}, {w.horizon_label_fr}, {METRIC_SHORT[w.metric] ?? w.metric}
          {w.p_holm != null ? `, p Holm ${fmtP(w.p_holm)}` : ''} · {EVALUATION_SHORT[w.evaluation] ?? w.evaluation}
        </span>
      </button>
      <span className="sc-win-caveats">
        {key.map((c) => (
          <span key={c} className="sc-chip" title={vocab.caveats[c]}>
            {CAVEAT_SHORT[c] ?? c}
          </span>
        ))}
      </span>
    </li>
  );
}

const CAVEAT_SHORT: Record<string, string> = {
  timeliness_edge: 'avantage de fraîcheur',
  uses_official_forecast: 'utilise la prévision officielle',
  final_vintage_outcome: 'issue en vintage final',
  same_institution_outcome: 'issue = estimation du FMI',
  licence_restricted: 'licence à vérifier',
};

function Headline({ p, onOpenTrack }: { p: Page; onOpenTrack: (id: string, row?: string) => void }) {
  const h = p.headline;
  const weakWins = p.weak_reference.bat;
  return (
    <section className="sc-headline" aria-labelledby="sc-headline-title" data-testid="headline">
      <h2 id="sc-headline-title" className="sc-headline-title">
        Contre les prévisionnistes officiels, REVA <span className="sc-hl-win">bat {h.bat}</span> · <span className="sc-hl-loss">ne bat pas {h.ne_bat_pas}</span> ·{' '}
        <span className="sc-hl-tie">égalité {h.egalite}</span> · <span className="sc-hl-pend">en attente {h.en_attente}</span>
      </h2>
      <p className="sc-headline-sub">
        {total(h).toLocaleString('fr-FR')} comparaisons jugées (chacune : une cible, un horizon, une métrique, une famille Holm). {p.headline_rule_fr}
      </p>
      <CountsBar c={h} label={`Répartition : ${h.bat} bat, ${h.ne_bat_pas} ne bat pas, ${h.egalite} égalité, ${h.en_attente} en attente`} />
      <div className="sc-headline-grid">
        <div>
          <h3 className="sc-h3">Les {h.bat} victoires, avec leurs réserves</h3>
          {p.wins.length === 0 ? (
            <p className="v8-muted">Aucune victoire contre un prévisionniste officiel.</p>
          ) : (
            <ul className="sc-wins">
              {p.wins.map((w) => (
                <WinLine key={w.id} w={w} vocab={p.vocab} onOpenTrack={onOpenTrack} />
              ))}
            </ul>
          )}
        </div>
        <div className="sc-headline-side">
          <h3 className="sc-h3">Par type d’évaluation</h3>
          <table className="sc-mini">
            <tbody>
              {Object.entries(p.by_evaluation).map(([k, c]) => (
                <tr key={k}>
                  <th scope="row">{EVALUATION_SHORT[k] ?? k}</th>
                  <td className="v8-num sc-hl-win">{c.bat || ''}</td>
                  <td className="v8-num sc-hl-loss">{c.ne_bat_pas || ''}</td>
                  <td className="v8-num sc-hl-tie">{c.egalite || ''}</td>
                  <td className="v8-num sc-hl-pend">{c.en_attente || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="sc-footnote">
            Comptés à part : {weakWins} « victoire{weakWins > 1 ? 's' : ''} » V6 contre un <strong>repère faible</strong> (densité trop large autour du point FMI/SPF — jamais « REVA bat le FMI ») ; références naïves : {p.naive.bat} bat,{' '}
            {p.naive.ne_bat_pas} ne bat pas, {p.naive.en_attente} en attente.
          </p>
          {(p.problems.length > 0 || p.stale_sources.length > 0) && (
            <p className="sc-warn" role="alert">
              {p.quarantined.length > 0 ? `${p.quarantined.length} ligne(s) en quarantaine (source modifiée, verdict non compté). ` : ''}
              {p.stale_sources.length > 0 ? `${p.stale_sources.length} fichier(s) source modifié(s) depuis la génération : régénérer le registre.` : ''}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------------------------------------ live + calendar
function LivePanel({ live }: { live: Load<LiveState> }) {
  if (live.status === 'loading')
    return (
      <Panel title="Registre prospectif (en direct)" subtitle="prévisions scellées avant l’issue">
        <Skeleton lines={3} />
      </Panel>
    );
  if (live.status === 'error')
    return (
      <Panel title="Registre prospectif (en direct)">
        <ErrorState error={live.error} what="l’état du registre" />
      </Panel>
    );
  const l = live.data;
  const t = l.totals;
  const resolved = t.resolved;
  const ens = l.ensemble;
  return (
    <Panel
      title="Registre prospectif (en direct)"
      subtitle={`prévisions scellées avant l’issue · calculé ${dayLabel(l.computed_at)} ${l.computed_at.slice(11, 16)} UTC${l.refreshing ? ' · mise à jour en cours' : ''}`}
      id="registre"
    >
      <div className="sc-stats">
        <Stat label="Lots scellés" value={t.sealed_batches ?? null} size="sm" missing={{ reason: 'non disponible' }} />
        <Stat label="Questions" value={t.questions ?? null} size="sm" missing={{ reason: 'non disponible' }} />
        <Stat label="Prévisions scellées" value={t.forecasts ?? null} size="sm" missing={{ reason: 'non disponible' }} />
        <Stat label="Résolues" value={resolved ?? null} size="sm" missing={{ reason: l.partial ? 'calcul en cours' : 'non disponible' }} />
      </div>
      {!l.partial && resolved === 0 && (
        <p className="sc-note" role="note">
          <strong>Aucune question résolue :</strong> pas encore de preuve prospective, dans un sens ou dans l’autre. Rien ne peut être rétro-rempli.
        </p>
      )}
      {l.partial && <p className="sc-note">Calcul complet en cours (lecture de chaque lot scellé) : statuts et calendrier dans un instant.</p>}
      {ens && ens.available && (
        <p className="sc-note">
          Ensemble REVA : {(ens.n_ensemble_forecasts ?? 0).toLocaleString('fr-FR')} prévisions scellées, {ens.n_ensemble_scored ?? 0} notées, {ens.cells ?? 0} cellules du tableau{' '}
          {Object.entries(ens.verdicts ?? {})
            .map(([k, v]) => `${v} ${k === 'awaiting_resolutions' ? 'en attente de résolutions' : k}`)
            .join(', ')}
          .
        </p>
      )}
    </Panel>
  );
}

function CalendarPanel({ items, live, onOpenTrack, onOpenDoc, today }: { items: CalendarItem[]; live: Load<LiveState>; onOpenTrack: (id: string) => void; onOpenDoc: (p: string) => void; today: Date }) {
  const upcoming = live.status === 'ok' ? live.data.upcoming : [];
  const firstDates = useMemo(() => {
    const by = new Map<string, { date: string; n: number; fams: string[]; officials: Set<string>; overdue: boolean }>();
    for (const u of upcoming) {
      const e = by.get(u.date) ?? { date: u.date, n: 0, fams: [], officials: new Set<string>(), overdue: u.overdue };
      e.n += u.n_questions;
      e.fams.push(u.label_fr ?? u.family);
      u.officials.forEach((o) => e.officials.add(o));
      by.set(u.date, e);
    }
    return [...by.values()];
  }, [upcoming]);
  const overdue = firstDates.filter((d) => d.overdue);
  const next = firstDates.filter((d) => !d.overdue).slice(0, 8);
  return (
    <Panel title="Calendrier des résolutions" subtitle="tests pré-enregistrés en attente, puis questions scellées du registre" id="calendrier">
      <ol className="sc-cal" data-testid="calendar">
        {items.map((g) => {
          const iso = g.date ?? g.not_before ?? (g.month ? `${g.month}-01` : null);
          const dd = daysUntil(iso, today);
          return (
            <li key={g.id ?? `${g.track}-${g.sort}`} className={`sc-cal-item sc-cal-${g.kind}`}>
              <span className="sc-cal-when">
                <span className="sc-cal-date">{whenLabel(g)}</span>
                {dd !== null && dd >= 0 && <span className="sc-cal-dd v8-num">J−{dd}</span>}
              </span>
              <span className="sc-cal-what">
                {g.kind === 'track' && g.track ? (
                  <button type="button" className="sc-link" onClick={() => onOpenTrack(g.track!)}>
                    {g.n_rows} test{g.n_rows > 1 ? 's' : ''} · {g.track}
                  </button>
                ) : (
                  <span className="sc-cal-event">jalon</span>
                )}{' '}
                {g.note_fr}
                {g.forecasters.length > 0 && <span className="v8-subtle"> · {g.forecasters.map(forecasterLabel).join(', ')}</span>}
                {g.sealed === true && <span className="sc-sealed"> · scellé</span>}
                {g.sealed === false && <span className="sc-unsealed"> · pas encore scellé</span>}
                {g.quote && (
                  <button type="button" className="sc-cite" title={`« ${g.quote.text} »`} onClick={() => onOpenDoc(g.quote!.file)}>
                    source
                  </button>
                )}
              </span>
            </li>
          );
        })}
      </ol>
      <h3 className="sc-h3">Prochaines questions scellées à résoudre (registre)</h3>
      {live.status === 'loading' && <Skeleton lines={3} />}
      {live.status === 'error' && <ErrorState error={live.error} what="le calendrier du registre" />}
      {live.status === 'ok' && live.data.partial && <p className="v8-muted">Calcul en cours…</p>}
      {live.status === 'ok' && !live.data.partial && (
        <>
          <ol className="sc-cal">
            {next.map((d) => (
              <li key={d.date} className="sc-cal-item sc-cal-registry">
                <span className="sc-cal-when">
                  <span className="sc-cal-date">{dayLabel(d.date)}</span>
                  <span className="sc-cal-dd v8-num">J−{daysUntil(d.date, today)}</span>
                </span>
                <span className="sc-cal-what">
                  {d.n.toLocaleString('fr-FR')} question{d.n > 1 ? 's' : ''} — {[...new Set(d.fams)].slice(0, 3).join(' ; ')}
                  {new Set(d.fams).size > 3 ? ` (+${new Set(d.fams).size - 3})` : ''}
                  {d.officials.size > 0 && <span className="v8-subtle"> · officiel scellé à côté : {[...d.officials].map(forecasterLabel).join(', ')}</span>}
                </span>
              </li>
            ))}
          </ol>
          {overdue.length > 0 && (
            <p className="sc-footnote">
              {overdue.reduce((a, d) => a + d.n, 0).toLocaleString('fr-FR')} questions ont une date attendue passée sans résolution encore (p. ex. {overdue[0]?.fams[0]}, attendue le {dayLabel(overdue[0]?.date ?? '')}) :
              en attente de la publication de la source.
            </p>
          )}
        </>
      )}
    </Panel>
  );
}

// ------------------------------------------------------------------------------------------------ explainer
function Explainer() {
  const items: [string, string][] = [
    ['À date égale', 'À la date t, REVA est comparée à la dernière prévision officielle publiée au plus tard à t — jamais à une prévision plus ancienne qu’elle n’aurait pas eue, ni plus récente.'],
    ['Vintages', 'Les données d’entrée sont lues telles que publiées à t (vintages archivés, règles de disponibilité prudentes) ; quand une issue est en vintage final, c’est écrit sur la ligne.'],
    ['Pré-enregistrement', 'Les hypothèses, le code (sha256) et la famille de tests sont commis avant d’ouvrir le holdout, ouvert une seule fois ; ou les prévisions sont scellées au registre avant l’issue.'],
    ['Scores propres', 'Points contre points (erreur absolue), densités contre densités (CRPS, WIS), probabilités contre probabilités (Brier) — jamais un point contre une densité.'],
    ['Holm', 'Une seule famille Holm par évaluation déclarée : une victoire exige p ajusté < 0,05 et un ratio < 1. Un test déclaré mais impossible compte p = 1.'],
    ['Ce qui ne compte pas', 'Le développement (données déjà vues), les repères faibles et les références naïves sont affichés mais jamais comptés comme « bat l’officiel ». Une combinaison qui utilise la prévision officielle se dit « la combinaison bat l’officiel seul ».'],
  ];
  return (
    <Panel title="Comment REVA est jugée" id="methode">
      <div className="sc-explain">
        {items.map(([t, d]) => (
          <div key={t} className="sc-explain-item">
            <h3 className="sc-h3">{t}</h3>
            <p>{d}</p>
          </div>
        ))}
      </div>
    </Panel>
  );
}

// ------------------------------------------------------------------------------------------------ track + cell
function TrackView({ id, vocab, onBack, onOpenDoc, highlight }: { id: string; vocab: Vocab; onBack: () => void; onOpenDoc: (p: string) => void; highlight: string }) {
  const [state, retry] = useLoad<TrackDetail>((s) => getTrack(id, s).then((r) => r.data), [id]);
  const [win, setWin] = useState<'all' | 'official' | 'naive'>('all');
  useTitle(state.status === 'ok' ? `${state.data.track.label_fr} · Scores` : 'Piste · Scores');
  useEffect(() => {
    if (state.status !== 'ok' || !highlight) return;
    document.querySelector(`[data-row="${CSS.escape(highlight)}"]`)?.scrollIntoView({ block: 'center' });
  }, [state.status, highlight]);
  if (state.status === 'loading') return <Skeleton height={420} />;
  if (state.status === 'error') return <ErrorState error={state.error} what="la piste" onRetry={retry} />;
  const d = state.data;
  const rows = d.rows.filter((r) => win === 'all' || (win === 'naive' ? r.benchmark_kind === 'naive' || r.benchmark_kind === 'weak_reference' : r.benchmark_kind !== 'naive' && r.benchmark_kind !== 'weak_reference'));
  const sorted = [...rows].sort((a, b) => order(a.verdict) - order(b.verdict) || (a.ratio ?? 9) - (b.ratio ?? 9));
  const charts = d.charts.filter((c) => c.kind !== 'pit').slice(0, 12);
  const pits = d.charts.filter((c) => c.kind === 'pit');
  return (
    <div className="sc-track" data-testid="track-view">
      <button type="button" className="sc-link sc-back" onClick={onBack}>
        ← Tableau des scores
      </button>
      <header className="sc-track-head">
        <h2 className="sc-track-title">{d.track.label_fr}</h2>
        <p className="v8-muted">{d.track.summary_fr}</p>
        <p className="sc-track-meta">
          <span>
            {d.track.agent} · {d.track.version}
          </span>
          {d.track.protocol ? (
            <button type="button" className="sc-link" onClick={() => onOpenDoc(d.track.protocol!.file)}>
              protocole <span className="v8-mono v8-subtle">sha256 {d.track.protocol.sha256?.slice(0, 10)}…</span>
            </button>
          ) : (
            <span className="v8-subtle">pas de protocole pré-enregistré</span>
          )}
          {d.track.report ? (
            <button type="button" className="sc-link" onClick={() => onOpenDoc(d.track.report!)}>
              rapport
            </button>
          ) : (
            <span className="sc-unsealed">rapport attendu ({d.track.report_expected}) pas encore publié</span>
          )}
        </p>
        <CountsBar c={d.counts} label="Répartition des verdicts contre les officiels sur cette piste" />
        <p className="sc-track-counts v8-num">
          Contre les officiels : {d.counts.bat} bat · {d.counts.ne_bat_pas} ne bat pas · {d.counts.egalite} égalité · {d.counts.en_attente} en attente
          {d.holm_families.length > 0 && <span className="v8-subtle"> · familles Holm : {d.holm_families.filter((f) => f.size).map((f) => `${f.window ?? '?'} m = ${f.size}`).join(', ')}</span>}
        </p>
      </header>
      {d.calendar.length > 0 && (
        <p className="sc-note">
          En attente : {d.calendar.map((g) => `${g.n_rows} test(s) — ${whenLabel(g)} (${g.note_fr})`).join(' ; ')}
        </p>
      )}
      {(charts.length > 0 || pits.length > 0) && (
        <Panel title="Graphiques" subtitle="copiés des fichiers de résultats ; descriptifs, la preuve est dans la table">
          <div className="sc-charts">
            {charts.map((c, i) => (
              <ChartView key={i} chart={c} />
            ))}
          </div>
          {pits.length > 0 && (
            <details className="sc-pits">
              <summary>Histogrammes PIT (calibration des densités, holdout) — {pits.length}</summary>
              <div className="sc-charts">
                {pits.map((c, i) => (
                  <ChartView key={i} chart={c} />
                ))}
              </div>
            </details>
          )}
        </Panel>
      )}
      <Panel
        title={`Toutes les comparaisons (${d.rows.length})`}
        subtitle="victoires et défaites ; cliquer une ligne pour sa base, ses réserves, sa source"
        flush
        actions={
          <div className="sc-seg-ctl" role="group" aria-label="Filtrer par repère">
            {(
              [
                ['all', 'Toutes'],
                ['official', 'Officiels'],
                ['naive', 'Naïf / faible'],
              ] as const
            ).map(([k, l]) => (
              <button key={k} type="button" className={`v8-btn v8-btn-sm${win === k ? ' v8-btn-primary' : ''}`} aria-pressed={win === k} onClick={() => setWin(k)}>
                {l}
              </button>
            ))}
          </div>
        }
      >
        <RowsTable rows={sorted} vocab={vocab} onOpenDoc={onOpenDoc} caption={`Comparaisons de la piste ${d.track.label_fr}`} />
      </Panel>
    </div>
  );
}

const order = (v: string) => ({ bat: 0, egalite: 1, ne_bat_pas: 2, en_attente: 3 })[v] ?? 4;

function CellDrawer({ k, title, vocab, onClose, onOpenDoc, onOpenTrack }: { k: string; title: string; vocab: Vocab; onClose: () => void; onOpenDoc: (p: string) => void; onOpenTrack: (id: string) => void }) {
  const [state] = useLoad<CellDetail>((s) => getCell(k, s).then((r) => r.data), [k]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  const forecaster = k.split('~')[1];
  return (
    <aside className="sc-drawer" aria-label="Détail de la cellule" data-testid="cell-drawer">
      <header className="sc-drawer-head">
        <div>
          <p className="sc-drawer-kicker">{forecaster === 'naive' ? 'référence naïve (comptée à part)' : forecasterLabel(forecaster)}</p>
          <h2 className="sc-drawer-title">{title}</h2>
        </div>
        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={onClose} aria-label="Fermer (Échap)">
          Fermer <KeyHint keys="Esc" />
        </button>
      </header>
      {state.status === 'loading' && <Skeleton lines={6} />}
      {state.status === 'error' && <ErrorState error={state.error} what="la cellule" />}
      {state.status === 'ok' && (
        <>
          <p className="sc-drawer-tracks">
            Pistes :{' '}
            {(state.data.cell.tracks ?? []).map((t) => (
              <button key={t} type="button" className="sc-link" onClick={() => onOpenTrack(t)}>
                {t}
              </button>
            ))}
          </p>
          <RowsTable rows={[...state.data.rows].sort((a, b) => order(a.verdict) - order(b.verdict))} vocab={vocab} onOpenDoc={onOpenDoc} caption="Comparaisons de la cellule" />
        </>
      )}
    </aside>
  );
}

function NoTrackDrawer({ cellKey, p, onClose, onOpenDoc }: { cellKey: string; p: Page; onClose: () => void; onOpenDoc: (p: string) => void }) {
  const found = p.matrix.flatMap((d) => d.targets.map((t) => ({ t, c: Object.values(t.cells).find((c) => c.key === cellKey) }))).find((x) => x.c);
  if (!found?.c) return null;
  return (
    <aside className="sc-drawer" aria-label="Pas encore de piste" data-testid="cell-drawer">
      <header className="sc-drawer-head">
        <div>
          <p className="sc-drawer-kicker">{forecasterLabel(found.c.forecaster ?? cellKey.split('~')[1])}</p>
          <h2 className="sc-drawer-title">{found.t.label_fr}</h2>
        </div>
        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={onClose}>
          Fermer
        </button>
      </header>
      <EmptyState title="Pas encore de piste" icon="◌">
        <p>{found.c.reason_fr}</p>
        {found.c.quote && (
          <p>
            <q className="sc-quote">{found.c.quote.text}</q>{' '}
            <button type="button" className="sc-link" onClick={() => onOpenDoc(found.c!.quote!.file)}>
              {found.c.quote.file}
            </button>
          </p>
        )}
      </EmptyState>
    </aside>
  );
}

function useTitle(t: string) {
  useEffect(() => {
    const prev = document.title;
    document.title = t;
    return () => {
      document.title = prev;
    };
  }, [t]);
}

// ------------------------------------------------------------------------------------------------ page
export default function ScoresPage() {
  const [track, setTrack] = useUrlParam('track');
  const [row, setRow] = useUrlParam('row');
  const [cell, setCell] = useUrlParam('cell');
  const [doc, setDoc] = useUrlParam('doc');
  const [focusF, setFocusF] = useUrlParam('f');
  const [domain, setDomain] = useUrlParam('d');
  const [section] = useUrlParam('section');
  const [page, retry] = useLoad<Page>((s) => getScores(s).then((r) => r.data), []);
  const [live, setLive] = useState<Load<LiveState>>({ status: 'loading' });
  const today = useMemo(() => new Date(), []);
  useTitle(track ? 'Piste · Scores' : 'Scores');

  useEffect(() => {
    const ac = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () =>
      getLive(ac.signal)
        .then((r) => {
          setLive({ status: 'ok', data: r.data });
          if (r.data.partial || r.data.refreshing) timer = setTimeout(load, 4000);
        })
        .catch((error: unknown) => !ac.signal.aborted && setLive({ status: 'error', error }));
    load();
    return () => {
      ac.abort();
      if (timer) clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    if (page.status === 'ok' && section) document.getElementById(section)?.scrollIntoView({ block: 'start' });
  }, [page.status, section]);

  const openTrack = useCallback(
    (id: string, r?: string) => {
      setCell('');
      setTrack(id, { replace: false });
      setRow(r ?? '');
    },
    [setCell, setTrack, setRow],
  );
  const openDoc = useCallback((p: string) => setDoc(p, { replace: false }), [setDoc]);
  const closeDoc = useCallback(() => setDoc(''), [setDoc]);
  const closeCell = useCallback(() => setCell(''), [setCell]);

  return (
    <div className="v8-page sc-page">
      <header className="v8-page-head">
        <h1 className="v8-page-title">Scores</h1>
        <p className="v8-page-lede">
          Les prévisions chiffrées de REVA contre chaque prévisionniste officiel — FMI, SPF, EIA, CDC, ViEWS… — victoires <em>et</em> défaites, à date égale, sur des évaluations
          pré-enregistrées. <Badge kind="judged" size="xs" />
        </p>
      </header>
      {page.status === 'loading' && <Skeleton height={520} />}
      {page.status === 'error' && <ErrorState error={page.error} what="le tableau des scores" onRetry={retry} />}
      {page.status === 'ok' && (
        <>
          <Headline p={page.data} onOpenTrack={openTrack} />
          {track ? (
            <TrackView id={track} vocab={page.data.vocab} onBack={() => setTrack('', { replace: false })} onOpenDoc={openDoc} highlight={row} />
          ) : (
            <>
              <Panel
                title="Tableau : cibles × prévisionnistes officiels"
                subtitle={`${page.data.n_rows} comparaisons au registre ; chaque cellule s’ouvre sur ses lignes`}
                flush
                actions={
                  <div className="sc-filters">
                    <label className="v8-sr-only" htmlFor="sc-f">
                      Prévisionniste
                    </label>
                    <select id="sc-f" className="v8-input sc-select" value={focusF} onChange={(e) => setFocusF(e.target.value, { replace: false })}>
                      <option value="">Tous les prévisionnistes</option>
                      {page.data.forecasters.map((f) => (
                        <option key={f.id} value={f.id}>
                          {f.label_fr}
                        </option>
                      ))}
                    </select>
                    <label className="v8-sr-only" htmlFor="sc-d">
                      Domaine
                    </label>
                    <select id="sc-d" className="v8-input sc-select" value={domain} onChange={(e) => setDomain(e.target.value, { replace: false })}>
                      <option value="">Tous les domaines</option>
                      {page.data.matrix.map((d) => (
                        <option key={d.domain} value={d.domain}>
                          {d.label_fr}
                        </option>
                      ))}
                    </select>
                  </div>
                }
              >
                <ForecasterFocus p={page.data} f={focusF} />
                <Matrix matrix={page.data.matrix} forecasters={page.data.forecasters} onOpen={(k) => setCell(k, { replace: false })} activeKey={cell} focusForecaster={focusF} domainFilter={domain} />
              </Panel>
              <div className="sc-two">
                <CalendarPanel items={page.data.calendar} live={live} onOpenTrack={(id) => openTrack(id)} onOpenDoc={openDoc} today={today} />
                <div className="sc-col">
                  <LivePanel live={live} />
                  <Panel title="Pistes" subtitle="chaque piste : son protocole, son rapport, toutes ses lignes">
                    <ul className="sc-tracks">
                      {page.data.tracks.map((t) => (
                        <li key={t.id}>
                          <button type="button" className="sc-track-btn" onClick={() => openTrack(t.id)}>
                            <span className="sc-track-name">{t.label_fr}</span>
                            <span className="sc-track-c v8-num">
                              {t.counts && total(t.counts) > 0 ? (
                                <>
                                  <span className="sc-hl-win">{t.counts.bat}</span> / <span className="sc-hl-loss">{t.counts.ne_bat_pas}</span> / <span className="sc-hl-tie">{t.counts.egalite}</span> /{' '}
                                  <span className="sc-hl-pend">{t.counts.en_attente}</span>
                                </>
                              ) : (
                                <span className="v8-subtle">vs naïf seulement</span>
                              )}
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                    <p className="sc-help">bat / ne bat pas / égalité / en attente, contre les officiels.</p>
                  </Panel>
                </div>
              </div>
              <Explainer />
              <p className="sc-help">
                Registre lisible par machine : <span className="v8-mono">registry/scoreboard/tracks.yaml</span> (généré depuis {page.data.sources.length} fichiers de résultats et rapports ; chaque victoire est re-vérifiée contre sa source).{' '}
                <a href={v8Links.ask('Pourquoi REVA ne bat-elle pas le FMI sur le PIB ?')}>Poser une question</a>
              </p>
            </>
          )}
          {cell && <Drawer cell={cell} p={page.data} onClose={closeCell} onOpenDoc={openDoc} onOpenTrack={openTrack} />}
          {doc && <DocViewer path={doc} onClose={closeDoc} />}
        </>
      )}
    </div>
  );
}

function Drawer({ cell, p, onClose, onOpenDoc, onOpenTrack }: { cell: string; p: Page; onClose: () => void; onOpenDoc: (p: string) => void; onOpenTrack: (id: string) => void }) {
  const targetId = cell.split('~')[0] ?? cell;
  const target = p.matrix.flatMap((d) => d.targets).find((t) => t.target === targetId);
  const c = target ? (Object.values(target.cells).find((x) => x.key === cell) ?? (target.naive?.key === cell ? target.naive : null)) : null;
  if (c?.status === 'no_track') return <NoTrackDrawer cellKey={cell} p={p} onClose={onClose} onOpenDoc={onOpenDoc} />;
  return <CellDrawer k={cell} title={target?.label_fr ?? targetId} vocab={p.vocab} onClose={onClose} onOpenDoc={onOpenDoc} onOpenTrack={onOpenTrack} />;
}

function ForecasterFocus({ p, f }: { p: Page; f: string }) {
  if (!f) return null;
  const fc = p.forecasters.find((x) => x.id === f);
  const cells = p.matrix.flatMap((d) => d.targets.map((t) => t.cells[f])).filter((x): x is NonNullable<typeof x> => Boolean(x));
  const c = cells.reduce<Counts>(
    (a, x) => ({ bat: a.bat + (x.counts?.bat ?? 0), ne_bat_pas: a.ne_bat_pas + (x.counts?.ne_bat_pas ?? 0), egalite: a.egalite + (x.counts?.egalite ?? 0), en_attente: a.en_attente + (x.counts?.en_attente ?? 0) }),
    { bat: 0, ne_bat_pas: 0, egalite: 0, en_attente: 0 },
  );
  const noTrack = cells.filter((x) => x.status === 'no_track').length;
  return (
    <div className="sc-focus" data-testid="forecaster-focus">
      <p>
        <strong>REVA contre {fc?.label_fr ?? f}</strong> : {total(c) === 0 && noTrack ? 'pas encore de piste' : `${c.bat} bat · ${c.ne_bat_pas} ne bat pas · ${c.egalite} égalité · ${c.en_attente} en attente`}
        {fc?.product ? <span className="v8-subtle"> · {fc.product}</span> : null}
      </p>
      <CountsBar c={c} label={`REVA contre ${fc?.label_fr ?? f}`} />
    </div>
  );
}

