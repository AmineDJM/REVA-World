/**
 * Every judged comparison of a track or a cell, wins AND losses: verdict, target/horizon, candidate vs benchmark,
 * evaluation kind, metric, ratio, raw and Holm p, sample, caveats. A row opens to its basis, caveats, source
 * pointer (file + JSON pointer + sha256), verbatim quote and resolution rule.
 */
import { Fragment, useState } from 'react';
import { Badge, MissingValue } from '../../shell/ui';
import type { Row, Vocab } from './api';
import { EVALUATION_SHORT, METRIC_SHORT, VERDICT_FR, VERDICT_TONE, fmtP, fmtPct, fmtRange, fmtRatio, forecasterLabel, notCountedReason, whenLabel } from './model';

export function VerdictBadge({ v }: { v: Row['verdict'] }) {
  const tone = VERDICT_TONE[v];
  if (tone === 'pending') return <span className="sc-badge sc-badge-pending">{VERDICT_FR[v]}</span>;
  return (
    <Badge kind={tone} size="xs">
      {VERDICT_FR[v]}
    </Badge>
  );
}

function sampleText(s: Row['sample']): string | null {
  if (!s) return null;
  const parts: string[] = [];
  const n = (k: string) => (typeof s[k] === 'number' ? (s[k] as number).toLocaleString('fr-FR') : null);
  if (n('n_origins')) parts.push(`${n('n_origins')} origines`);
  if (n('n_issues')) parts.push(`${n('n_issues')} numéros`);
  if (n('n_rounds')) parts.push(`${n('n_rounds')} tours`);
  if (n('n_pairs')) parts.push(`${n('n_pairs')} paires`);
  if (n('n') && !parts.length) parts.push(`n ${n('n')}`);
  if (n('n_tests')) parts.push(`${n('n_tests')} tests`);
  return parts.join(' · ') || null;
}

export function RowsTable({ rows, vocab, onOpenDoc, caption }: { rows: Row[]; vocab: Vocab; onOpenDoc: (path: string) => void; caption: string }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="sc-table-box">
      <table className="sc-table">
        <caption className="v8-sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Verdict</th>
            <th scope="col">Comparaison</th>
            <th scope="col">Éval.</th>
            <th scope="col" className="sc-num">
              Ratio
            </th>
            <th scope="col" className="sc-num" title="p brut unilatéral (REVA meilleure)">
              p
            </th>
            <th scope="col" className="sc-num" title="p ajusté de Holm sur la famille déclarée (m = taille)">
              p Holm (m)
            </th>
            <th scope="col">Réserves</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const isOpen = open === r.id;
            const ratio = fmtRatio(r.ratio) ?? fmtRange(r.ratio_range?.[0], r.ratio_range?.[1]);
            const nc = notCountedReason(r);
            return (
              <Fragment key={r.id}>
                <tr className={`sc-tr sc-tr-${r.verdict}${isOpen ? ' sc-tr-open' : ''}${r.quarantined ? ' sc-tr-quarantined' : ''}`} data-row={r.id}>
                  <td>
                    <VerdictBadge v={r.verdict} />
                  </td>
                  <td className="sc-cmp">
                    <button type="button" className="sc-rowbtn" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : r.id)}>
                      <span className="sc-cmp-cand">{r.candidate_label_fr ?? r.candidate}</span>
                      <span className="sc-cmp-vs"> vs </span>
                      <span className="sc-cmp-bench">{r.benchmark_label_fr ?? r.benchmark}</span>
                    </button>
                    <span className="sc-cmp-meta">
                      {forecasterLabel(r.forecaster)} · {r.horizon_label_fr ?? r.horizon} · {METRIC_SHORT[r.metric] ?? r.metric}
                      {sampleText(r.sample) ? ` · ${sampleText(r.sample)}` : ''}
                      {nc ? <span className="sc-nc"> · {nc}</span> : null}
                    </span>
                  </td>
                  <td>
                    <span className="sc-eval" title={vocab.evaluations[r.evaluation] ?? r.evaluation}>
                      {EVALUATION_SHORT[r.evaluation] ?? r.evaluation}
                    </span>
                  </td>
                  <td className="sc-num v8-num">{ratio ?? (r.verdict === 'en_attente' ? <span className="v8-subtle">à venir</span> : <MissingValue reason="non publié dans la source" compact />)}</td>
                  <td className="sc-num v8-num">{fmtP(r.p_raw) ?? <span className="v8-subtle">·</span>}</td>
                  <td className="sc-num v8-num">
                    {fmtP(r.p_holm) ?? <span className="v8-subtle">·</span>}
                    {r.holm_family_size ? <span className="v8-subtle"> ({r.holm_family_size})</span> : null}
                  </td>
                  <td>
                    <span className="sc-caveat-count" title={r.caveats.map((c) => vocab.caveats[c] ?? c).join('\n')}>
                      {r.caveats.length ? `${r.caveats.length} ⚑` : ''}
                    </span>
                  </td>
                </tr>
                {isOpen && (
                  <tr className="sc-tr-detail">
                    <td colSpan={7}>
                      <RowDetail r={r} vocab={vocab} onOpenDoc={onOpenDoc} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function RowDetail({ r, vocab, onOpenDoc }: { r: Row; vocab: Vocab; onOpenDoc: (path: string) => void }) {
  return (
    <div className="sc-detail" data-testid="row-detail">
      {r.quarantined && (
        <p className="sc-warn" role="alert">
          En quarantaine : {(r.problems ?? []).join(' ; ')}
        </p>
      )}
      <p className="sc-basis">
        <strong>Base du verdict :</strong> {r.verdict_basis_fr ?? 'non indiquée'}
      </p>
      <dl className="sc-dl">
        <dt>Évaluation</dt>
        <dd>{vocab.evaluations[r.evaluation] ?? r.evaluation}</dd>
        <dt>Candidat</dt>
        <dd>
          {r.candidate_label_fr} <span className="v8-subtle">({vocab.candidate_kinds[r.candidate_kind] ?? r.candidate_kind})</span>
        </dd>
        <dt>Repère</dt>
        <dd>
          {r.benchmark_label_fr} <span className="v8-subtle">({vocab.benchmark_kinds[r.benchmark_kind] ?? r.benchmark_kind})</span>
        </dd>
        <dt>Métrique</dt>
        <dd>{vocab.metrics[r.metric] ?? r.metric}</dd>
        {r.coverage && (
          <>
            <dt>Couverture</dt>
            <dd className="v8-num">
              {Object.entries(r.coverage)
                .map(([k, v]) => `${fmtPct(Number(k))} → ${fmtPct(v) ?? 'non publiée'}`)
                .join(' · ')}
              {r.benchmark_coverage ? ` (officiel : ${Object.entries(r.benchmark_coverage).map(([k, v]) => `${fmtPct(Number(k))} → ${fmtPct(v) ?? 'non publiée'}`).join(' · ')})` : ''}
            </dd>
          </>
        )}
        {r.resolution && (
          <>
            <dt>Résolution</dt>
            <dd>
              {whenLabel(r.resolution)} — {r.resolution.note_fr}
              {r.sealed === true && <span className="sc-sealed"> · scellé au registre</span>}
              {r.sealed === false && <span className="sc-unsealed"> · pas encore de lot scellé</span>}
            </dd>
          </>
        )}
        <dt>Source</dt>
        <dd>
          <span className="v8-mono">{r.source.file}</span>
          {r.source.pointer ? <span className="v8-mono v8-subtle"> #{r.source.pointer}</span> : null}
          {r.source.sha256 ? <span className="v8-mono v8-subtle" title={r.source.sha256}> · sha256 {r.source.sha256.slice(0, 10)}…</span> : null}
        </dd>
        {(r.source.quote || r.verdict_quote) && (
          <>
            <dt>Citation</dt>
            <dd>
              <q className="sc-quote">{r.verdict_quote?.text ?? r.source.quote}</q>{' '}
              <button type="button" className="sc-link" onClick={() => onOpenDoc(r.verdict_quote?.file ?? r.source.file)}>
                {r.verdict_quote?.file ?? r.source.file}
              </button>
            </dd>
          </>
        )}
        {r.protocol && (
          <>
            <dt>Protocole</dt>
            <dd>
              <button type="button" className="sc-link" onClick={() => onOpenDoc(r.protocol!.file)}>
                {r.protocol.file}
              </button>
              {r.protocol.sha256 ? <span className="v8-mono v8-subtle"> · sha256 {r.protocol.sha256.slice(0, 10)}…</span> : null}
            </dd>
          </>
        )}
        {r.report && (
          <>
            <dt>Rapport</dt>
            <dd>
              <button type="button" className="sc-link" onClick={() => onOpenDoc(r.report!)}>
                {r.report}
              </button>
            </dd>
          </>
        )}
      </dl>
      {r.caveats.length > 0 && (
        <ul className="sc-caveats">
          {r.caveats.map((c) => (
            <li key={c}>
              <span className="sc-caveat-dot" aria-hidden="true">
                ⚑
              </span>{' '}
              {vocab.caveats[c] ?? c}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
