import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { useApi } from '../../lib/useApi';
import { warningsFr } from '../api/common';
import {
  getBacktests,
  getCalibration,
  getForecastBatches,
  getForecastFamilies,
  getForecastQuestion,
  getForecastQuestions,
  getLeaderboard,
  type ForecastRecord,
  type QuestionSummary,
} from '../api/forecasts';
import { V3Empty, V3Error, V3Loading } from '../components/States';
import { frDecimal } from '../lib/format';

const TYPE_FR: Record<string, string> = { quantity: 'quantité', interval: 'intervalle 80 %', binary: 'oui / non' };
const STATUS_FR: Record<string, string> = {
  pending: 'en attente',
  resolved: 'résolue',
  void: 'annulée (aucune valeur publiée)',
  not_prospective: 'non prospective (exclue)',
};
const FAMILY_STATUS_FR: Record<string, string> = {
  available: 'disponible',
  awaiting_table: 'table attendue',
  awaiting_source: 'source à créer',
};

export function modelFr(key: string): string {
  const m: Record<string, string> = {
    'forecast.registry_persistence': 'Persistance (marche aléatoire)',
    'forecast.registry_seasonal_naive': 'Saisonnier naïf',
    'forecast.registry_ar': 'AR(p) estimé à la date d’émission',
    'forecast.registry_hist_quantile': 'Quantiles historiques',
    'forecast.reference_imf_weo': 'FMI — Perspectives (WEO), attribué',
  };
  return m[key.split('@')[0]!] ?? key;
}

/** French number or an explicit "n. a." (not applicable is never shown as 0). */
export function fr(n: number | null | undefined, digits = 3): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return 'n. a.';
  return n.toLocaleString('fr-FR', { maximumFractionDigits: digits });
}

function frStr(v: string | null | undefined, digits = 3): string {
  return v === null || v === undefined ? 'n. a.' : frDecimal(v, digits);
}

export default function PrevisionsPage() {
  const [params, setParams] = useSearchParams();
  const qid = params.get('q');
  const family = params.get('famille') ?? '';
  const qtype = params.get('type') ?? '';
  const status = params.get('statut') ?? '';
  const [offset, setOffset] = useState(0);
  const set = (k: string, v: string) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v);
    else p.delete(k);
    setParams(p);
    setOffset(0);
  };

  const fams = useApi((s) => getForecastFamilies(s), []);
  const batches = useApi((s) => getForecastBatches(s), []);
  const lb = useApi((s) => getLeaderboard(s), []);
  const qs = useApi(
    (s) => getForecastQuestions({ family: family || undefined, type: qtype || undefined, status: status || undefined, limit: 50, offset }, s),
    [family, qtype, status, offset],
  );
  const warnings = useMemo(() => (lb.status === 'ok' ? warningsFr(lb.data.warnings) : []), [lb.status, lb.data]);

  return (
    <section className="v3-page v3-previsions" aria-labelledby="previsions-title">
      <header className="v3-page-head">
        <h1 id="previsions-title">Prévisions</h1>
        <p className="v3-lede">
          Registre prospectif : des questions datées sur des séries publiées (inflation, chômage, croissance, taux, change, prix), des
          prévisions <strong>gelées et scellées avant la résolution</strong>, puis notées quand la source déclarée publie la valeur.
        </p>
      </header>
      <div className="v3-notice" role="note" data-testid="previsions-honesty">
        <p>
          <strong>Prévisions de référence (baselines) uniquement</strong> : persistance, saisonnier naïf, AR(p), quantiles historiques,
          et la projection du FMI (WEO) citée comme telle. Ce ne sont pas des modèles validés : le registre sert à mesurer, avant toute
          affirmation de compétence. Ce n’est pas un conseil financier.
        </p>
        {warnings.length > 0 && (
          <ul>
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        )}
      </div>

      <h2>Lots scellés</h2>
      {batches.status === 'loading' && <V3Loading what="des lots" />}
      {batches.status === 'error' && <V3Error error={batches.error} what="Les lots de prévisions" onRetry={batches.reload} />}
      {batches.status === 'ok' &&
        (batches.data.data.length === 0 ? (
          <V3Empty>
            <p>Aucune prévision émise pour l’instant.</p>
          </V3Empty>
        ) : (
          <table className="v3-table" data-testid="previsions-batches">
            <thead>
              <tr>
                <th>Cycle</th>
                <th>Gelé le</th>
                <th>Questions</th>
                <th>Sceau (sha-256)</th>
              </tr>
            </thead>
            <tbody>
              {batches.data.data.map((b) => (
                <tr key={b.batch_sha256}>
                  <td>{b.cycle}</td>
                  <td>{b.issued_at ? new Date(b.issued_at).toLocaleString('fr-FR', { timeZone: 'UTC' }) + ' UTC' : 'n. a.'}</td>
                  <td>
                    {b.n_questions} ({Object.entries(b.by_type).map(([t, n]) => `${n} ${TYPE_FR[t] ?? t}`).join(', ')})
                  </td>
                  <td>
                    <code title={b.batch_sha256}>{b.batch_sha256.slice(0, 16)}…</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}

      <h2>Classement</h2>
      {lb.status === 'loading' && <V3Loading what="du classement" />}
      {lb.status === 'error' && <V3Error error={lb.error} what="Le classement" onRetry={lb.reload} />}
      {lb.status === 'ok' && (
        <>
          <p data-testid="previsions-counts">
            Questions : {Object.entries(lb.data.data.questions).map(([k, n]) => `${n} ${STATUS_FR[k] ?? k}`).join(' · ') || 'aucune'}.
            {(lb.data.data.questions.resolved ?? 0) === 0 && ' Aucune question n’est encore résolue : les scores apparaîtront à mesure que les sources publient.'}
          </p>
          <table className="v3-table" data-testid="previsions-leaderboard">
            <thead>
              <tr>
                <th>Prévisionniste</th>
                <th>Émises</th>
                <th>Abstentions</th>
                <th>Notées</th>
                <th>EAM</th>
                <th>CRPS</th>
                <th>Brier</th>
                <th>Couverture 80 %</th>
                <th>EAM / persistance</th>
              </tr>
            </thead>
            <tbody>
              {lb.data.data.models.map((m) => (
                <tr key={m.model}>
                  <td title={m.description}>
                    {modelFr(m.model)}
                    {m.kind === 'external_reference' && <span className="v3-badge v3-badge-declared"> source externe</span>}
                  </td>
                  <td>{m.n_issued}</td>
                  <td>{m.n_abstained}</td>
                  <td>{m.n_scored}</td>
                  <td>{fr(m.mae)}</td>
                  <td>{fr(m.crps)}</td>
                  <td>{fr(m.brier)}</td>
                  <td>{m.coverage === null ? 'n. a.' : `${fr(m.coverage * 100, 1)} %`}</td>
                  <td>{fr(m.mae_vs_persistence.ratio)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <CalibrationBlock />
        </>
      )}

      <h2>Questions</h2>
      <div className="v3-toolbar">
        <label>
          Famille{' '}
          <select value={family} onChange={(e) => set('famille', e.target.value)} data-testid="previsions-family">
            <option value="">toutes</option>
            {fams.status === 'ok' &&
              fams.data.data
                .filter((f) => f.status === 'available')
                .map((f) => (
                  <option key={f.key} value={f.key}>
                    {f.label_fr}
                  </option>
                ))}
          </select>
        </label>
        <label>
          Type{' '}
          <select value={qtype} onChange={(e) => set('type', e.target.value)}>
            <option value="">tous</option>
            {Object.entries(TYPE_FR).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label>
          Statut{' '}
          <select value={status} onChange={(e) => set('statut', e.target.value)}>
            <option value="">tous</option>
            {Object.entries(STATUS_FR).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
      </div>
      {qs.status === 'loading' && <V3Loading what="des questions" />}
      {qs.status === 'error' && <V3Error error={qs.error} what="Les questions" onRetry={qs.reload} />}
      {qs.status === 'ok' && (
        <QuestionTable
          rows={qs.data.data.questions}
          total={qs.data.data.total}
          offset={offset}
          onPage={setOffset}
          onOpen={(id) => set('q', id)}
          selected={qid}
        />
      )}
      {qid && <QuestionPanel id={qid} onClose={() => set('q', '')} />}

      <h2>Familles de questions</h2>
      {fams.status === 'loading' && <V3Loading what="des familles" />}
      {fams.status === 'error' && <V3Error error={fams.error} what="Les familles de questions" onRetry={fams.reload} />}
      {fams.status === 'ok' && (
        <table className="v3-table" data-testid="previsions-families">
          <thead>
            <tr>
              <th>Variable</th>
              <th>Source de résolution</th>
              <th>Fréquence</th>
              <th>Statut</th>
            </tr>
          </thead>
          <tbody>
            {fams.data.data.map((f) => (
              <tr key={f.key}>
                <td title={f.definition ?? undefined}>{f.label_fr}</td>
                <td>
                  <code>{f.dataset_key ?? '—'}</code>
                </td>
                <td>{{ M: 'mensuelle', Q: 'trimestrielle', A: 'annuelle' }[f.frequency]}</td>
                <td>
                  {FAMILY_STATUS_FR[f.status]}
                  {f.status_reason ? ` — ${f.status_reason}` : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <BacktestBlock />
    </section>
  );
}

function QuestionTable({
  rows,
  total,
  offset,
  onPage,
  onOpen,
  selected,
}: {
  rows: QuestionSummary[];
  total: number;
  offset: number;
  onPage: (o: number) => void;
  onOpen: (id: string) => void;
  selected: string | null;
}) {
  if (total === 0)
    return (
      <V3Empty>
        <p>Aucune question ne correspond à ces filtres.</p>
      </V3Empty>
    );
  return (
    <>
      <table className="v3-table" data-testid="previsions-questions">
        <thead>
          <tr>
            <th>Question</th>
            <th>Période</th>
            <th>Type</th>
            <th>Statut</th>
            <th>Résolution attendue</th>
            <th>Valeur publiée</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((q) => (
            <tr key={q.question_id} aria-selected={selected === q.question_id}>
              <td>
                <button type="button" className="v3-link" onClick={() => onOpen(q.question_id)}>
                  {q.text_fr}
                </button>
              </td>
              <td>{q.reference_period}</td>
              <td>{TYPE_FR[q.type]}</td>
              <td>{STATUS_FR[q.status]}</td>
              <td>{q.expected_resolution}</td>
              <td>{q.outcome === null ? '—' : frDecimal(q.outcome, 3)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="v3-pager">
        {offset + 1}–{offset + rows.length} sur {total}{' '}
        <button type="button" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - 50))}>
          Précédentes
        </button>{' '}
        <button type="button" disabled={offset + rows.length >= total} onClick={() => onPage(offset + 50)}>
          Suivantes
        </button>
      </p>
    </>
  );
}

function QuestionPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const d = useApi((s) => getForecastQuestion(id, s), [id]);
  if (d.status === 'loading') return <V3Loading what="de la question" />;
  if (d.status === 'error') return <V3Error error={d.error} what="La question" onRetry={d.reload} />;
  if (d.status !== 'ok') return null;
  const { question: q, forecasts, outcome, seal } = d.data.data;
  return (
    <aside className="v3-card" aria-label="Détail de la question" data-testid="previsions-detail">
      <button type="button" onClick={onClose} aria-label="Fermer">
        ×
      </button>
      <h3>{String(q.text_fr)}</h3>
      {q.definition ? <p className="muted">Définition : « {String(q.definition)} »</p> : null}
      <p>
        Scellé le {seal.issued_at ? new Date(seal.issued_at).toLocaleString('fr-FR', { timeZone: 'UTC' }) + ' UTC' : 'n. a.'} — sceau{' '}
        <code>{seal.batch_sha256.slice(0, 16)}…</code> {seal.verified ? '(vérifié à la lecture)' : ''}.
      </p>
      <p>
        Résolution : {String((q.resolution as Record<string, unknown>)?.note ?? '')}{' '}
        {outcome ? `— statut : ${STATUS_FR[String(outcome.status)] ?? String(outcome.status)}${outcome.value ? `, valeur ${frDecimal(String(outcome.value), 3)}` : ''}` : '— en attente.'}
      </p>
      <table className="v3-table">
        <thead>
          <tr>
            <th>Prévisionniste</th>
            <th>Point</th>
            <th>Intervalle 80 %</th>
            <th>P(&gt; seuil)</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody>
          {forecasts.map((f: ForecastRecord) => (
            <tr key={f.model}>
              <td>{modelFr(f.model)}</td>
              {f.status === 'abstained' ? (
                <td colSpan={4} className="muted">
                  Abstention : {f.reason}
                </td>
              ) : (
                <>
                  <td>{frStr(f.point)}</td>
                  <td>{f.interval ? `[${frStr(f.interval[0])} ; ${frStr(f.interval[1])}]` : 'n. a.'}</td>
                  <td>{f.prob_above ? `${frDecimal(String(Number(f.prob_above) * 100), 1)} %` : 'n. a.'}</td>
                  <td>
                    {f.score
                      ? ['abs_error', 'crps', 'brier', 'covered']
                          .filter((k) => f.score && f.score[k] !== undefined && f.score[k] !== null)
                          .map((k) => `${k} ${String(f.score![k])}`)
                          .join(' · ')
                      : 'pas encore notée'}
                  </td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </aside>
  );
}

function CalibrationBlock() {
  const [model, setModel] = useState('forecast.registry_persistence');
  const c = useApi((s) => getCalibration(model, s), [model]);
  return (
    <details className="v3-calibration">
      <summary>Calibration (histogramme PIT, couverture des intervalles)</summary>
      <select value={model} onChange={(e) => setModel(e.target.value)} aria-label="Prévisionniste">
        {['forecast.registry_persistence', 'forecast.registry_seasonal_naive', 'forecast.registry_ar', 'forecast.registry_hist_quantile'].map((m) => (
          <option key={m} value={m}>
            {modelFr(m)}
          </option>
        ))}
      </select>
      {c.status === 'error' && <V3Error error={c.error} what="La calibration" />}
      {c.status === 'ok' &&
        (c.data.data.n_scored === 0 ? (
          <p>Aucune prévision notée : pas encore de calibration.</p>
        ) : (
          <p>
            PIT ({c.data.data.pit_histogram.n}) : {c.data.data.pit_histogram.counts.join(' · ')} — couverture empirique des intervalles 80 % :{' '}
            {c.data.data.interval_coverage.empirical === null ? 'n. a.' : `${fr(c.data.data.interval_coverage.empirical * 100, 1)} %`}
          </p>
        ))}
    </details>
  );
}

function BacktestBlock() {
  const bt = useApi((s) => getBacktests(s), []);
  return (
    <>
      <h2>Rétro-test pseudo temps réel (millésime final)</h2>
      <p className="v3-notice" role="note">
        Chaque estimation n’utilise que les périodes antérieures à son origine, mais sur les données <strong>révisées actuelles</strong>, pas
        telles que publiées à l’époque : ce n’est <strong>pas</strong> une compétence en temps réel.
      </p>
      {bt.status === 'loading' && <V3Loading what="du rétro-test" />}
      {bt.status === 'error' && <V3Error error={bt.error} what="Le rétro-test" onRetry={bt.reload} />}
      {bt.status === 'ok' &&
        (bt.data.data.length === 0 ? (
          <V3Empty>
            <p>Aucun rétro-test enregistré.</p>
          </V3Empty>
        ) : (
          <table className="v3-table" data-testid="previsions-backtest">
            <thead>
              <tr>
                <th>Famille</th>
                <th>Prévisionniste</th>
                <th>Horizon (périodes)</th>
                <th>N</th>
                <th>EAM / persistance</th>
                <th>CRPS / persistance</th>
                <th>Couverture 80 %</th>
              </tr>
            </thead>
            <tbody>
              {bt.data.data[0]!.families.flatMap((f) =>
                f.results.map((r) => (
                  <tr key={`${f.family}-${r.model}-${r.horizon_periods}`}>
                    <td>{f.family}</td>
                    <td>{modelFr(r.model)}</td>
                    <td>{r.horizon_periods}</td>
                    <td>{r.n_scored}</td>
                    <td>{fr(r.mae_vs_persistence.ratio)}</td>
                    <td>{fr(r.crps_vs_persistence.ratio)}</td>
                    <td>{r.coverage === null ? 'n. a.' : `${fr(r.coverage * 100, 1)} %`}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        ))}
    </>
  );
}
