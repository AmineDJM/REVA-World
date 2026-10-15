import { useMemo, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router';
import { createScenario, createScenarioDraft, getBeliefReleases, getBeliefs, newIdempotencyKey, startSimulation } from '../api/client';
import { ApiError } from '../api/problem';
import type { BaselineReleaseRef, Fidelity } from '../api/types';
import type { ScenarioDraft } from '../api/v2types';
import { ContextBanner } from '../components/ContextBanner';
import { ExperimentalLabel } from '../components/HonestyLabels';
import { MODEL_KEYS } from '../api/v2types';
import { SpecEditor } from '../components/SpecEditor';
import { ErrorState, Loading, ProblemView } from '../components/StateViews';
import { STATUS_TEXT, diffJson, normalizeDraft, pinToRelease, reviewSpec, setIn, type DiffEntry } from '../lib/draft';
import { formatDateTime, humanize, shortId } from '../lib/format';
import { validateBaselineReleaseId } from '../lib/scenario';
import { useApi } from '../lib/useApi';
import { MissingInputs } from './SimulationPage';

export const DRAFT_NOTICE = 'Draft written by a language model. Nothing runs until you confirm.';
const MAX_TEXT = 2000;

function DiffView({ entries, empty }: { entries: DiffEntry[]; empty: string }) {
  if (entries.length === 0) return <p className="muted">{empty}</p>;
  return (
    <div className="table-wrap">
      <table className="data-table json-diff" data-testid="json-diff">
        <caption>
          {entries.length} change{entries.length === 1 ? '' : 's'} (path, before, after)
        </caption>
        <thead>
          <tr>
            <th scope="col">Path</th>
            <th scope="col">Change</th>
            <th scope="col">Before</th>
            <th scope="col">After</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((d) => (
            <tr key={`${d.path}-${d.kind}`} className={`diff-${d.kind}`}>
              <th scope="row" className="mono">
                {d.path}
              </th>
              <td>{d.kind}</td>
              <td className="mono diff-before">{d.kind === 'added' ? '—' : JSON.stringify(d.before)}</td>
              <td className="mono diff-after">{d.kind === 'removed' ? '—' : JSON.stringify(d.after)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ProviderLine({ d }: { d: ScenarioDraft }) {
  const u = d.usage ?? {};
  const tok = (k: string) => (typeof u[k] === 'number' ? String(u[k]) : '—');
  return (
    <dl className="context-fields provider-line" data-testid="draft-provider">
      <div>
        <dt>Drafted by</dt>
        <dd>
          <span data-testid="draft-model">
            {d.provider ?? 'provider not stated'} · {d.model ?? 'model not stated'}
          </span>
          {d.model_requested && d.model_requested !== d.model && <span className="muted"> (requested {d.model_requested})</span>}
        </dd>
      </div>
      {d.fallback_from.length > 0 && (
        <div>
          <dt>Fallback</dt>
          <dd>
            after {d.fallback_from.map((f) => String(f.provider ?? '?')).join(', ')} failed
          </dd>
        </div>
      )}
      {d.prompt_version && (
        <div>
          <dt>Prompt</dt>
          <dd className="mono" title={d.prompt_sha256 ?? undefined}>
            {d.prompt_version}
            {d.prompt_sha256 && ` · ${shortId(d.prompt_sha256)}`}
          </dd>
        </div>
      )}
      <div>
        <dt>Tokens</dt>
        <dd>
          {tok('input_tokens')} in · {tok('output_tokens')} out
        </dd>
      </div>
      {d.latency_ms !== null && (
        <div>
          <dt>Latency</dt>
          <dd>{Math.round(d.latency_ms)} ms</dd>
        </div>
      )}
    </dl>
  );
}

const DRAFT_ERROR_TEXT: Record<string, string> = {
  llm_unavailable: 'Drafting is unavailable on this server (disabled, not configured, or its budget ledger is missing). Nothing was sent to a language model.',
  budget_exhausted: 'The daily drafting budget is used up. Nothing was sent to a language model; try again tomorrow or write the scenario by hand.',
  llm_provider_error: 'The language-model provider failed and no draft was produced. No fallback guess is shown.',
  payload_too_large: 'The description is too long (at most 4096 bytes).',
  no_published_release: 'No published belief release exists; a draft must be pinned to one.',
  idempotency_conflict: 'This request key was already used for a different text. Draft again.',
};

/** A drafting failure as a clear, typed message (never a silent fallback). */
function DraftError({ error }: { error: unknown }) {
  const api = error instanceof ApiError ? error : null;
  const missing = api && (api.status === 404 || api.status === 405);
  const text = api ? (DRAFT_ERROR_TEXT[api.code ?? ''] ?? null) : null;
  return (
    <div className="state state-error" role="alert" data-testid="draft-error" data-code={api?.code ?? ''}>
      <p>
        <strong>
          {missing
            ? 'Drafting is not available on this server'
            : api
              ? `Draft refused (HTTP ${api.status}${api.code ? `, ${api.code}` : ''})`
              : 'Drafting failed'}
        </strong>
        {missing && <> (POST /v1/scenario-drafts returned HTTP {api!.status})</>}.{' '}
        {text ?? api?.problem.detail ?? (error instanceof Error ? error.message : String(error))}
      </p>
      {api?.problem.detail && text && <p className="muted">{api.problem.detail}</p>}
      <p>
        You can write the scenario by hand on the <Link to="/simulation">Simulation</Link> screen.
      </p>
    </div>
  );
}

/** "a.b[0].c" -> ["a", "b", 0, "c"] */
export function splitPath(path: string): (string | number)[] {
  const out: (string | number)[] = [];
  for (const part of path.split('.')) {
    const m = /^([^[\]]+)((?:\[\d+\])*)$/.exec(part);
    if (!m) {
      out.push(part);
      continue;
    }
    out.push(m[1]!);
    for (const idx of m[2]!.matchAll(/\[(\d+)\]/g)) out.push(Number(idx[1]));
  }
  return out;
}

function getIn(obj: unknown, parts: (string | number)[]): unknown {
  let cur: unknown = obj;
  for (const p of parts) {
    if (cur === null || typeof cur !== 'object') return undefined;
    cur = (cur as Record<string | number, unknown>)[p];
  }
  return cur;
}

/** An input for a field the draft is missing. Numbers stay numbers; nothing is pre-filled. */
function MissingFieldInput({ path, spec, onSet }: { path: string; spec: Record<string, unknown>; onSet: (parts: (string | number)[], v: unknown) => void }) {
  const parts = splitPath(path);
  const current = getIn(spec, parts);
  const [text, setText] = useState(current === undefined || current === null ? '' : String(current));
  const id = `missing-${path.replace(/[^\w]/g, '_')}`;
  return (
    <div className="spec-field">
      <label htmlFor={id}>{path}</label>
      <input
        id={id}
        type="text"
        value={text}
        placeholder="required — not set"
        onChange={(e) => {
          const t = e.target.value;
          setText(t);
          const trimmed = t.trim();
          onSet(parts, trimmed === '' ? undefined : /^-?\d+(\.\d+)?$/.test(trimmed) ? Number(trimmed) : trimmed);
        }}
      />
    </div>
  );
}

const PROVENANCE_TEXT: Record<string, string> = {
  stated: 'stated in your text',
  converted_from_stated: 'converted from a number you stated',
  not_in_text: 'NOT in your text — check it',
  server_default: 'server default',
  schema_constant: 'fixed by the schema',
};

/**
 * Scenario composer (V2 step 2): free text -> POST /v1/scenario-drafts -> review (restatement,
 * readable spec form, JSON diff, capability gaps) -> Confirm alone submits POST /v1/scenarios
 * and starts the run. The language model only drafts; it never computes or runs anything.
 */
export default function ComposerPage() {
  const navigate = useNavigate();
  const beliefs = useApi((s) => getBeliefs({ limit: 1 }, s), []);
  const releases = useApi((s) => getBeliefReleases({ limit: 20 }, s), []);
  const [chosenRelease, setChosenRelease] = useState('');
  const [text, setText] = useState('');
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<unknown>(null);
  const [draft, setDraft] = useState<ScenarioDraft | null>(null);
  const [previous, setPrevious] = useState<ScenarioDraft | null>(null);
  const [edited, setEdited] = useState<Record<string, unknown> | null>(null);
  const [confirming, setConfirming] = useState<null | 'creating' | 'starting'>(null);
  const [confirmError, setConfirmError] = useState<unknown>(null);
  const keys = useRef<{ spec: string; scenario: string; simulation: string } | null>(null);
  const abort = useRef<AbortController | null>(null);

  const releaseOptions: BaselineReleaseRef[] = useMemo(() => {
    const list: BaselineReleaseRef[] = (releases.data?.data ?? []).map((r) => ({
      id: r.id,
      sequence: r.sequence,
      knowledge_cutoff: r.knowledge_cutoff,
      published_at: r.published_at,
      origin: r.origin,
      method: r.method,
    }));
    const latest = beliefs.data?.baseline_release;
    if (latest && !list.some((r) => r.id === latest.id)) list.unshift(latest);
    return list;
  }, [releases.data, beliefs.data]);
  const release = releaseOptions.find((r) => r.id === (chosenRelease || beliefs.data?.baseline_release?.id)) ?? null;
  const releaseError = beliefs.status === 'loading' && !release ? null : validateBaselineReleaseId(release?.id);

  const confirmedSpec = useMemo(() => (edited ? pinToRelease(edited, release) : null), [edited, release]);
  const baseSpec = draft ? reviewSpec(draft) : null;
  const userDiff = useMemo(() => (baseSpec && confirmedSpec ? diffJson(baseSpec, confirmedSpec) : []), [baseSpec, confirmedSpec]);
  const userEdited = useMemo(() => (baseSpec && edited ? diffJson(baseSpec, edited).length > 0 : false), [baseSpec, edited]);
  const prevSpec = previous ? reviewSpec(previous) : null;
  const redraftDiff = useMemo(() => (prevSpec && baseSpec ? diffJson(prevSpec, baseSpec) : null), [prevSpec, baseSpec]);
  const fidelity = (typeof confirmedSpec?.fidelity === 'string' ? confirmedSpec.fidelity : 'L1') as Fidelity;
  const ready = draft?.status === 'draft_ready' && !!draft.draft_spec;
  // A candidate (incomplete/invalid) spec can be submitted only after the user edited it; the server re-validates.
  const canConfirm = !!confirmedSpec && (ready || userEdited) && !releaseError && !!release && !confirming && !drafting;
  const validationErrors = Array.isArray(draft?.validation.errors) ? (draft!.validation.errors as unknown[]) : [];

  const submitDraft = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim() || drafting) return;
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    setDrafting(true);
    setDraftError(null);
    setConfirmError(null);
    try {
      const body = await createScenarioDraft({ text: text.trim(), baseline_release_id: release?.id }, ctrl.signal);
      const d = normalizeDraft(body);
      setPrevious(draft);
      setDraft(d);
      const spec = reviewSpec(d);
      setEdited(spec ? { ...spec } : null);
      keys.current = null;
    } catch (err) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) setDraftError(err);
    } finally {
      setDrafting(false);
    }
  };

  const confirm = async () => {
    if (!canConfirm || !confirmedSpec) return;
    setConfirmError(null);
    const specJson = JSON.stringify(confirmedSpec);
    if (!keys.current || keys.current.spec !== specJson) keys.current = { spec: specJson, scenario: newIdempotencyKey(), simulation: newIdempotencyKey() };
    try {
      setConfirming('creating');
      const created = await createScenario(confirmedSpec, keys.current.scenario);
      setConfirming('starting');
      const accepted = await startSimulation({ scenario_id: created.scenario_id, fidelity }, keys.current.simulation);
      navigate(`/runs/${accepted.job_id}`);
    } catch (err) {
      setConfirmError(err);
    } finally {
      setConfirming(null);
    }
  };


  return (
    <>
      <h1>Scenario composer</h1>
      <div className="llm-notice" role="note" data-testid="llm-notice">
        <strong>{DRAFT_NOTICE}</strong> The language model turns your text into a draft scenario specification only. It does not
        compute results, create evidence or choose data values; the server validates the draft with the same schema as a hand-written
        scenario, and parts it cannot express are listed as capability gaps instead of being guessed.
      </div>
      <ContextBanner
        meta={{
          context_mode: 'simulation',
          modality: 'assumption_conditioned_scenario',
          origin: release?.origin ?? 'none',
          baseline_release: release,
          knowledge_cutoff: release?.knowledge_cutoff ?? null,
          warnings: beliefs.data?.warnings ?? [],
        }}
        simulation
        scope="Scenario draft (nothing has run)"
      />

      <form className="panel" onSubmit={submitDraft} aria-labelledby="compose-h">
        <h2 id="compose-h">Describe a scenario</h2>
        {beliefs.status === 'loading' && <Loading what="published baseline release" />}
        {beliefs.status === 'error' && <ErrorState error={beliefs.error} what="The baseline release" onRetry={beliefs.reload} />}
        {releaseOptions.length > 0 && (
          <label>
            Pinned belief release
            <select value={release?.id ?? ''} onChange={(e) => setChosenRelease(e.target.value)}>
              {releaseOptions.map((r) => (
                <option key={r.id} value={r.id}>
                  #{r.sequence ?? '—'} · cutoff {formatDateTime(r.knowledge_cutoff)} · {r.origin === 'test_fixture' ? 'TEST FIXTURE' : r.origin} · {shortId(r.id)}
                </option>
              ))}
            </select>
          </label>
        )}
        {releaseError && (
          <p role="alert" className="field-error">
            {releaseError}
          </p>
        )}
        <label>
          Scenario description
          <textarea
            value={text}
            maxLength={MAX_TEXT}
            rows={4}
            onChange={(e) => setText(e.target.value)}
            aria-describedby="compose-hint"
            placeholder="e.g. Crude import prices rise 25% for France, China and the United States in 2023, with a sensitivity analysis."
          />
          <span id="compose-hint" className="field-hint">
            English or French (« Hausse de 25 % du prix du brut importé pour la France en 2023 »). {text.length}/{MAX_TEXT} characters. The text is sent
            to the server's drafting service only; it is treated as data, never as instructions.
          </span>
        </label>
        <p>
          <button type="submit" className="primary" disabled={!text.trim() || drafting}>
            {drafting ? 'Drafting…' : draft ? 'Draft again' : 'Draft scenario'}
          </button>{' '}
          <span className="field-hint">Drafting does not create a scenario or start a run.</span>
        </p>
        {drafting && <Loading what="draft from the language model" />}
        {!!draftError && <DraftError error={draftError} />}
      </form>

      {draft && (
        <section className="panel draft-panel" aria-labelledby="draft-h" data-testid="draft-review">
          <h2 id="draft-h">Review the draft</h2>
          <p className="llm-badge-line">
            <span className="llm-badge">LLM draft</span> {DRAFT_NOTICE}
          </p>
          <ProviderLine d={draft} />
          {(() => {
            const mk = typeof baseSpec?.model_key === 'string' ? baseSpec.model_key : null;
            const flagged = draft.warnings.find((w) => w.startsWith('experimental_model'));
            return flagged || (mk && mk !== MODEL_KEYS.crude) ? <ExperimentalLabel reviewStatus={flagged ? 'maintainer_review_pending' : null} /> : null;
          })()}

          <p className={`draft-status status-${draft.status}`} data-testid="draft-status" data-status={draft.status}>
            <strong>{STATUS_TEXT[draft.status] ?? humanize(draft.status)}</strong>
            {draft.executed === false && <span className="muted"> · nothing executed</span>}
          </p>

          <h3>Restatement of the draft</h3>
          <blockquote className="restatement" lang={draft.language === 'fr' ? 'fr' : draft.language === 'en' ? 'en' : undefined} data-testid="restatement">
            {draft.restatement || <span className="muted">No restatement returned.</span>}
          </blockquote>
          <p className="field-hint">Written by the server from the validated draft, not by the language model.</p>
          {draft.llm_restatement && (
            <details>
              <summary>The language model's own paraphrase (untrusted, for comparison)</summary>
              <blockquote className="restatement restatement-llm" lang={draft.language === 'fr' ? 'fr' : undefined}>
                {draft.llm_restatement}
              </blockquote>
            </details>
          )}

          {draft.unsupported.length > 0 && (
            <>
              <h3>Not supported (capability gaps)</h3>
              <ul className="gap-chips" aria-label="Unsupported parts of the request" data-testid="capability-gaps">
                {draft.unsupported.map((u, i) => (
                  <li key={i} className={`gap-chip gap-${u.kind ?? 'capability_gap'}`} title={u.reason ?? undefined}>
                    <span className="gap-chip-kind">{u.kind === 'policy_refusal' ? 'refused' : 'capability gap'}</span>
                    <span className="gap-chip-key mono">{u.capability_key ?? 'unclassified'}</span>
                    {u.text && <span className="gap-chip-text">“{u.text}”</span>}
                    {u.reason && <span className="gap-chip-reason">{u.reason}</span>}
                    {u.source && <span className="gap-chip-source">flagged by {u.source}</span>}
                  </li>
                ))}
              </ul>
              <p className="field-hint">These parts are left out of the draft. Nothing approximates them.</p>
            </>
          )}

          {draft.missing_fields.length > 0 && (
            <div data-testid="missing-fields">
              <p className="field-error">
                Missing before this can run: {draft.missing_fields.map((f) => <code key={f}>{f} </code>)}. Nothing is filled in for you; enter
                each value below (the server validates it).
              </p>
              {edited && (
                <div className="spec-grid">
                  {draft.missing_fields.map((f) => (
                    <MissingFieldInput key={f} path={f} spec={edited} onSet={(parts, v) => setEdited((cur) => setIn(cur, parts, v) as Record<string, unknown>)} />
                  ))}
                </div>
              )}
            </div>
          )}

          {draft.numbers_to_confirm.length > 0 && (
            <div className="table-wrap">
              <table className="data-table" data-testid="numbers-to-confirm">
                <caption>Numbers to confirm, with where each came from</caption>
                <thead>
                  <tr>
                    <th scope="col">Parameter</th>
                    <th scope="col">Value</th>
                    <th scope="col">Origin</th>
                    <th scope="col">From your text</th>
                  </tr>
                </thead>
                <tbody>
                  {draft.numbers_to_confirm.map((n, i) => (
                    <tr key={`${n.path}-${i}`} className={n.provenance === 'not_in_text' ? 'number-unstated' : undefined}>
                      <th scope="row" className="mono">
                        {n.path}
                      </th>
                      <td className="num">{JSON.stringify(n.value)}</td>
                      <td>{n.provenance ? (PROVENANCE_TEXT[n.provenance] ?? humanize(n.provenance)) : 'not stated'}</td>
                      <td>{n.quote ? `“${n.quote}”` : <span className="muted">no quote</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {draft.assumptions_added.length > 0 && (
            <>
              <h3>Choices made for you</h3>
              <ul data-testid="assumptions-added">
                {draft.assumptions_added.map((a, i) => (
                  <li key={i}>
                    {a.text}
                    {a.source && <span className="muted"> ({a.source})</span>}
                  </li>
                ))}
              </ul>
            </>
          )}

          {draft.warnings.length > 0 && (
            <ul className="context-warnings" aria-label="Draft warnings">
              {draft.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}

          {validationErrors.length > 0 && (
            <div className="problem" role="alert" data-testid="draft-invalid">
              <p>
                <strong>The validator rejected this draft:</strong>
              </p>
              <ul>
                {validationErrors.map((e, i) => (
                  <li key={i}>{typeof e === 'object' && e && 'message' in e ? String((e as { message: unknown }).message) : JSON.stringify(e)}</li>
                ))}
              </ul>
            </div>
          )}

          {!baseSpec && (
            <p className="muted" role="status" data-testid="no-spec">
              No specification to review: nothing in the text could be drafted as a scenario a registered model supports.
            </p>
          )}
          {baseSpec && !ready && (
            <p className="field-hint" role="note">
              This is a candidate for review only. Edit it to complete it; Confirm stays disabled until you change something, and the server
              validates it again.
            </p>
          )}

          {edited && (
            <>
              <h3>Draft specification</h3>
              <SpecEditor spec={edited} onChange={(path, v) => setEdited((s) => setIn(s, path, v) as Record<string, unknown>)} disabled={!!confirming} />
              <h3>Changes before confirming</h3>
              <DiffView entries={userDiff} empty="No changes: you are confirming the draft exactly as the model wrote it (plus the pinned release)." />
              {redraftDiff && (
                <details>
                  <summary>Difference from the previous draft ({redraftDiff.length})</summary>
                  <DiffView entries={redraftDiff} empty="The new draft is identical to the previous one." />
                </details>
              )}
              <details>
                <summary>Specification JSON that Confirm will submit</summary>
                <pre className="json" data-testid="confirm-json">
                  {JSON.stringify(confirmedSpec, null, 2)}
                </pre>
              </details>
            </>
          )}

          <div className="confirm-row">
            <button type="button" className="primary" onClick={confirm} disabled={!canConfirm} aria-describedby="confirm-hint">
              {confirming === 'creating' ? 'Creating scenario…' : confirming === 'starting' ? 'Starting run…' : `Confirm and run (${fidelity})`}
            </button>
            <span id="confirm-hint" className="field-hint">
              {!confirmedSpec
                ? 'Nothing to confirm.'
                : releaseError
                  ? 'Blocked: no valid baseline release.'
                  : !ready && !userEdited
                    ? 'Blocked: the draft is not ready; edit the candidate first.'
                    : 'Creates the scenario (POST /v1/scenarios) and queues the run. Only this button runs anything.'}
            </span>
          </div>
          {!!confirmError && <MissingInputs error={confirmError} />}
          {!!confirmError && <ProblemView error={confirmError} />}
        </section>
      )}
    </>
  );
}
