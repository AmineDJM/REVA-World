import { useState, type FormEvent } from 'react';
import { getConnectors, getProposals, newIdempotencyKey, reviewProposal, startDiscovery } from '../api/client';
import type { ConnectorOut, DiscoveryRequest, Proposal, ReviewDecision } from '../api/types';
import { ContextBanner } from '../components/ContextBanner';
import { EnvelopeDetails } from '../components/EnvelopeDetails';
import { JobTracker } from '../components/JobTracker';
import { JsonView } from '../components/JsonView';
import { ProvenanceButton } from '../components/ProvenanceDrawer';
import { Empty, ErrorState, Loading, ProblemView } from '../components/StateViews';
import { formatDateTime, humanize, isRecord, shortId } from '../lib/format';
import { useApi } from '../lib/useApi';

/** Collect nested values whose key looks like a semantic diff. */
export function findDiffs(obj: unknown, path = ''): { path: string; value: unknown }[] {
  const out: { path: string; value: unknown }[] = [];
  if (!isRecord(obj)) return out;
  for (const [k, v] of Object.entries(obj)) {
    const p = path ? `${path}.${k}` : k;
    if (/diff|differences|mismatch/i.test(k)) out.push({ path: p, value: v });
    else if (isRecord(v)) out.push(...findDiffs(v, p));
  }
  return out;
}

function findProfiles(obj: unknown): { source: Record<string, unknown>; target: Record<string, unknown> } | null {
  if (!isRecord(obj)) return null;
  if (isRecord(obj.source_semantic_profile) && isRecord(obj.target_semantic_profile)) {
    return { source: obj.source_semantic_profile, target: obj.target_semantic_profile };
  }
  for (const v of Object.values(obj)) {
    const r = findProfiles(v);
    if (r) return r;
  }
  return null;
}

function HealthVector({ c }: { c: ConnectorOut }) {
  const h = c.latest_health;
  if (!h) return <span className="muted">no health record</span>;
  return (
    <div>
      <span className={`tag health-${h.status}`}>{humanize(h.status)}</span>{' '}
      <span className="muted">as of {formatDateTime(h.recorded_at)}</span>
      <JsonView value={h.metrics} />
    </div>
  );
}

function SemanticDiff({ summary }: { summary: Record<string, unknown> }) {
  const profiles = findProfiles(summary);
  const diffs = findDiffs(summary);
  if (!profiles && diffs.length === 0) return <p className="muted">No semantic diff recorded in this proposal.</p>;
  const keys = profiles ? [...new Set([...Object.keys(profiles.source), ...Object.keys(profiles.target)])].sort() : [];
  return (
    <>
      {profiles && (
        <div className="table-wrap">
          <table className="data-table">
            <caption>Semantic profile: source vs target concept</caption>
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">Source</th>
                <th scope="col">Target</th>
                <th scope="col">Same?</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => {
                const a = JSON.stringify(profiles.source[k] ?? null);
                const b = JSON.stringify(profiles.target[k] ?? null);
                return (
                  <tr key={k} className={a !== b ? 'diff-row' : undefined}>
                    <th scope="row">{humanize(k)}</th>
                    <td>{profiles.source[k] === undefined ? <span className="muted">absent</span> : <JsonView value={profiles.source[k]} />}</td>
                    <td>{profiles.target[k] === undefined ? <span className="muted">absent</span> : <JsonView value={profiles.target[k]} />}</td>
                    <td>{a === b ? 'same' : <strong>differs</strong>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {diffs.map((d) => (
        <div key={d.path}>
          <h5 className="mono">{d.path}</h5>
          <JsonView value={d.value} />
        </div>
      ))}
    </>
  );
}

function ReviewForm({ proposal, onDone }: { proposal: Proposal; onDone: () => void }) {
  const [decision, setDecision] = useState<ReviewDecision>('reject');
  const [comment, setComment] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const id = `review-${proposal.id}`;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!comment.trim()) {
      setError(new Error('A review comment is required.'));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await reviewProposal(proposal.id, decision, comment.trim(), newIdempotencyKey());
      setDone(`Recorded ${humanize(r.decision)} by ${r.reviewer}.`);
      onDone();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} aria-labelledby={`${id}-h`} className="review-form">
      <h4 id={`${id}-h`}>Review</h4>
      <fieldset>
        <legend>Decision</legend>
        <div className="inline-checks">
          {(['approve', 'reject', 'request_changes'] as ReviewDecision[]).map((d) => (
            <label key={d}>
              <input type="radio" name={`${id}-decision`} checked={decision === d} onChange={() => setDecision(d)} /> {humanize(d)}
            </label>
          ))}
        </div>
      </fieldset>
      <label>
        Comment (required)
        <textarea rows={3} cols={60} maxLength={4000} value={comment} onChange={(e) => setComment(e.target.value)} required />
      </label>
      <button type="submit" disabled={busy}>
        {busy ? 'Recording…' : 'Record review'}
      </button>
      {done && <p role="status">{done}</p>}
      {!!error && <ProblemView error={error} />}
    </form>
  );
}

function ProposalCard({ p, onReviewed }: { p: Proposal; onReviewed: () => void }) {
  const summary = p.summary ?? {};
  const title = typeof summary.title === 'string' ? summary.title : typeof summary.description === 'string' ? summary.description : p.proposal_type;
  return (
    <article className="panel" aria-label={`Proposal ${shortId(p.id)}`}>
      <h3>
        {humanize(p.proposal_type)}: {title}
      </h3>
      <p className="muted">
        <span className="mono" title={p.id}>
          {shortId(p.id)}
        </span>{' '}
        · tier T{p.permission_tier} · by {p.author} · {formatDateTime(p.recorded_at)} · manifest{' '}
        <ProvenanceButton kind="artifact" id={p.manifest_sha256} label={p.manifest_sha256.slice(0, 12)} />
      </p>
      <h4>Semantic diff</h4>
      <SemanticDiff summary={summary} />
      <h4>Promotion evidence (gate results)</h4>
      {p.gate_results.length === 0 ? (
        <p className="muted">No gate has evaluated this proposal.</p>
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <caption>Gate results</caption>
            <thead>
              <tr>
                <th scope="col">Gate</th>
                <th scope="col">Decision</th>
                <th scope="col">Policy</th>
                <th scope="col">Evaluator</th>
                <th scope="col">Evidence</th>
                <th scope="col">Recorded</th>
              </tr>
            </thead>
            <tbody>
              {p.gate_results.map((g) => (
                <tr key={g.id}>
                  <th scope="row">{humanize(g.gate_name)}</th>
                  <td>{humanize(g.decision)}</td>
                  <td className="mono">{g.policy_version}</td>
                  <td>{g.evaluator}</td>
                  <td>
                    <ProvenanceButton kind="artifact" id={g.evidence_sha256} label={g.evidence_sha256.slice(0, 12)} />
                  </td>
                  <td>{formatDateTime(g.recorded_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h4>Reviews</h4>
      {p.reviews.length === 0 ? (
        <p className="muted">Not yet reviewed.</p>
      ) : (
        <ul>
          {p.reviews.map((r) => (
            <li key={r.id}>
              <strong>{humanize(r.decision)}</strong> by {r.reviewer} ({formatDateTime(r.recorded_at)}): {r.comment || <span className="muted">no comment</span>}
            </li>
          ))}
        </ul>
      )}
      <details>
        <summary>Full proposal summary</summary>
        <JsonView value={summary} />
      </details>
      <ReviewForm proposal={p} onDone={onReviewed} />
    </article>
  );
}

function DiscoveryForm() {
  const [req, setReq] = useState<DiscoveryRequest>({ catalogue: 'eurostat', query: 'population on 1 January by age and sex', max_candidates: 10 });
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await startDiscovery({ ...req, concept_id: req.concept_id || null }, newIdempotencyKey());
      setJobId(r.job_id);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="form-grid" aria-label="Bounded catalogue discovery">
      <label>
        Official catalogue
        <select value={req.catalogue} onChange={(e) => setReq({ ...req, catalogue: e.target.value as DiscoveryRequest['catalogue'] })}>
          <option value="eurostat">Eurostat</option>
          <option value="worldbank">World Bank</option>
          <option value="comtrade">UN Comtrade</option>
        </select>
      </label>
      <label>
        Query
        <input type="text" minLength={2} maxLength={200} value={req.query} onChange={(e) => setReq({ ...req, query: e.target.value })} required />
      </label>
      <label>
        Target concept (optional)
        <input type="text" value={req.concept_id ?? ''} onChange={(e) => setReq({ ...req, concept_id: e.target.value })} />
      </label>
      <label>
        Max candidates
        <input
          type="number"
          min={1}
          max={50}
          value={req.max_candidates}
          onChange={(e) => setReq({ ...req, max_candidates: Math.max(1, Math.min(50, Number(e.target.value) || 1)) })}
        />
      </label>
      <div>
        <button type="submit" disabled={busy}>
          {busy ? 'Submitting…' : 'Start discovery'}
        </button>
      </div>
      {!!error && <ProblemView error={error} />}
      {jobId && <JobTracker jobId={jobId} label="Discovery" />}
    </form>
  );
}

export default function AcquisitionPage() {
  const connectors = useApi((s) => getConnectors(undefined, s), []);
  const proposals = useApi((s) => getProposals({ limit: 50 }, s), []);

  return (
    <>
      <h1>Acquisition</h1>
      <ContextBanner meta={connectors.data} scope="Connectors, discovery and proposals" />

      <section className="panel" aria-labelledby="conn-h">
        <h2 id="conn-h">Connector health</h2>
        {connectors.status === 'loading' && <Loading what="connectors" />}
        {connectors.status === 'error' && <ErrorState error={connectors.error} what="Connectors" onRetry={connectors.reload} />}
        {connectors.data && connectors.data.data.length === 0 && <Empty what="connectors" />}
        {connectors.data && connectors.data.data.length > 0 && (
          <div className="table-wrap">
            <table className="data-table">
              <caption>Registered connector versions and latest health vector</caption>
              <thead>
                <tr>
                  <th scope="col">Connector</th>
                  <th scope="col">Source</th>
                  <th scope="col">Licence</th>
                  <th scope="col">Acquisitions</th>
                  <th scope="col">Last retrieved</th>
                  <th scope="col">Health vector</th>
                </tr>
              </thead>
              <tbody>
                {connectors.data.data.map((c) => (
                  <tr key={c.id}>
                    <th scope="row">
                      {c.name}@{c.version}
                    </th>
                    <td>{c.source_key}</td>
                    <td>{c.licence ?? <span className="muted">not stated</span>}</td>
                    <td className="numeric">{c.acquisitions}</td>
                    <td>{c.last_retrieved_at ? formatDateTime(c.last_retrieved_at) : <span className="muted">never</span>}</td>
                    <td>
                      <HealthVector c={c} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {connectors.data && <EnvelopeDetails meta={connectors.data} />}
      </section>

      <section className="panel" aria-labelledby="disc-h">
        <h2 id="disc-h">Discovery</h2>
        <p className="muted">
          Bounded search of an official catalogue. Candidates become reviewable proposals; nothing is promoted without gates and a
          human review.
        </p>
        <DiscoveryForm />
      </section>

      <section className="panel" aria-labelledby="prop-h">
        <h2 id="prop-h">Proposals</h2>
        {proposals.status === 'loading' && <Loading what="proposals" />}
        {proposals.status === 'error' && <ErrorState error={proposals.error} what="Proposals" onRetry={proposals.reload} />}
        {proposals.data && proposals.data.data.length === 0 && <Empty what="proposals" />}
        {proposals.data?.data.map((p) => <ProposalCard key={p.id} p={p} onReviewed={proposals.reload} />)}
        {proposals.data?.next_cursor && <p className="bound-flag">More proposals exist than shown (50).</p>}
      </section>
    </>
  );
}
