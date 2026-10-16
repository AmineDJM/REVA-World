import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useSearchParams } from 'react-router';
import { NON_FACT_KINDS, postAsk, type AskEvidence, type AskResponse } from '../api/ask';
import { CommunityBadge, FixtureBadge, SourcedOnlyBanner } from '../components/Honesty';
import { V3Error, V3Loading } from '../components/States';
import { isWikidataSource, wikidataItemUrl, wikidataRevisionUrl, wikidataStatementUrl } from '../lib/citations';

const EXAMPLES = [
  'Quelle est la langue officielle du Brésil ?',
  'Comment Christophe Colomb est-il lié au Brésil ?',
  'Quel est le taux de chômage de la France en 2022 ?',
  'Quelle était la position des États-Unis sur la décolonisation dans les années 1960 ?',
];

type State = { status: 'idle' } | { status: 'loading' } | { status: 'ok'; data: AskResponse } | { status: 'error'; error: unknown };

export default function DemanderPage() {
  const [params, setParams] = useSearchParams();
  const [question, setQuestion] = useState(params.get('q') ?? '');
  const [state, setState] = useState<State>({ status: 'idle' });
  const ctrl = useRef<AbortController | null>(null);
  const qid = useId();

  const ask = (q: string) => {
    const text = q.trim();
    if (!text) return;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setState({ status: 'loading' });
    postAsk(text, 'fr', c.signal)
      .then((data) => !c.signal.aborted && setState({ status: 'ok', data }))
      .catch((error: unknown) => {
        if (!c.signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) setState({ status: 'error', error });
      });
  };

  useEffect(() => {
    const q = params.get('q');
    if (q) ask(q);
    return () => ctrl.current?.abort();
    // Run once for a deep link (?q=…); later questions go through the form.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const p = new URLSearchParams(params);
    p.set('q', question.trim());
    setParams(p, { replace: true });
    ask(question);
  };

  return (
    <section className="v3-page v3-demander" aria-labelledby="demander-title">
      <header className="v3-page-head">
        <h1 id="demander-title">Demander</h1>
        <p className="v3-lede">Posez une question sur le monde. La réponse est composée uniquement à partir du graphe de connaissances, de l’atlas et des liens, avec une citation par phrase.</p>
      </header>
      <SourcedOnlyBanner />
      <form className="v3-ask-form" onSubmit={submit}>
        <label htmlFor={qid}>Votre question</label>
        <textarea
          id={qid}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          rows={3}
          maxLength={1500}
          placeholder="ex. Quelle est la langue officielle du Brésil ?"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit(e);
          }}
        />
        <div className="v3-ask-actions">
          <button type="submit" className="primary" disabled={!question.trim() || state.status === 'loading'}>
            Demander
          </button>
          <span className="field-hint">Ctrl+Entrée pour envoyer. La question n’est pas conservée en clair par le serveur (empreinte seulement).</span>
        </div>
        <p className="v3-examples">
          Exemples :{' '}
          {EXAMPLES.map((ex) => (
            <button key={ex} type="button" className="v3-chip-button" onClick={() => setQuestion(ex)}>
              {ex}
            </button>
          ))}
        </p>
      </form>
      {state.status === 'loading' && <V3Loading what="de la réponse" />}
      {state.status === 'error' && <V3Error error={state.error} what="La réponse" />}
      {state.status === 'ok' && <Answer data={state.data} />}
    </section>
  );
}

export function Answer({ data }: { data: AskResponse }) {
  const evidence = new Map(data.evidence.map((e) => [e.id, e]));
  const [focus, setFocus] = useState<string | null>(null);
  return (
    <div className="v3-answer" data-testid="ask-answer">
      {data.restatement && (
        <p className="v3-meta">
          Question comprise comme : <em>{data.restatement}</em>
        </p>
      )}
      <div className="v3-answer-text" aria-live="polite">
        {data.sentences.length === 0 ? (
          <p className="v3-missing">Aucune phrase sourcée n’a pu être composée pour cette question.</p>
        ) : (
          data.sentences.map((s) => {
            const nonFact = NON_FACT_KINDS.has(s.kind);
            return (
              <p key={s.id} className={`v3-sentence v3-sentence-${s.kind}${nonFact ? ' v3-sentence-nonfact' : ''}`} data-testid="answer-sentence" data-kind={s.kind}>
                {s.text}{' '}
                {s.citations.length === 0 && !nonFact && s.kind !== 'note' ? (
                  <span className="v3-badge v3-badge-uncited">sans citation</span>
                ) : (
                  s.citations.map((c) => (
                    <a
                      key={c}
                      href={`#ev-${c}`}
                      className={`v3-cite-chip${focus === c ? ' active' : ''}`}
                      onClick={() => setFocus(c)}
                      title={evidence.get(c) ? undefined : 'preuve non fournie par le serveur'}
                      data-testid="citation-chip"
                    >
                      {c}
                    </a>
                  ))
                )}
              </p>
            );
          })
        )}
      </div>
      {data.unsupported.length > 0 && (
        <div className="v3-unsupported">
          <h3>Ce que la réponse ne couvre pas</h3>
          <ul>
            {data.unsupported.map((u, i) => (
              <li key={i}>{String(u.text ?? u.reason ?? JSON.stringify(u))}</li>
            ))}
          </ul>
        </div>
      )}
      <h3>Preuves citées ({data.evidence.length})</h3>
      <ol className="v3-evidence">
        {data.evidence.map((e) => (
          <EvidenceItem key={e.id} e={e} active={focus === e.id} />
        ))}
      </ol>
      <details className="v3-plan">
        <summary>
          Plan de requête ({data.plan.operations.length} opération{data.plan.operations.length > 1 ? 's' : ''}, planificateur : {data.planner ?? data.plan.planner ?? '?'})
        </summary>
        {data.llm_restatement && (
          <p className="field-hint">
            Reformulation du modèle (non fiable, pour comparaison) : <em>{data.llm_restatement}</em>
          </p>
        )}
        <pre className="json">{JSON.stringify(data.plan, null, 2)}</pre>
        {data.model && (
          <p className="muted">
            Modèle : {data.provider} {data.model} · prompt {data.prompt_version}
          </p>
        )}
      </details>
      {data.warnings.length > 0 && (
        <details className="v3-warnings-box">
          <summary>Avertissements ({data.warnings.length})</summary>
          <ul className="v3-warnings">
            {data.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function labelOf(x: unknown): string {
  if (!x || typeof x !== 'object') return typeof x === 'string' ? x : '';
  const o = x as Record<string, unknown>;
  const l = (o.labels ?? {}) as Record<string, string>;
  return l.fr || l.en || String(o.qid ?? o.id ?? '');
}

function EvidenceItem({ e, active }: { e: AskEvidence; active: boolean }) {
  const c = e.citation as Record<string, unknown>;
  const s = (k: string) => (typeof c[k] === 'string' || typeof c[k] === 'number' ? String(c[k]) : null);
  const x = e as Record<string, unknown>;
  const source = s('source') ?? '';
  const rawUrl = s('url');
  const url = rawUrl && /^https:\/\//.test(rawUrl) ? rawUrl : null;
  const wd = isWikidataSource(source) || (!!url && /^https:\/\/www\.wikidata\.org\//.test(url));
  const stmt = s('statement_id');
  const rev = s('revision') ?? s('source_revision');
  const href = url ?? (wd ? (wikidataStatementUrl(stmt, typeof x.qid === 'string' ? x.qid : null) ?? (typeof x.qid === 'string' ? wikidataItemUrl(x.qid) : null)) : null);
  const community = wd || c.status === 'community_edited';
  const kindFr: Record<string, string> = {
    kg_statement: 'déclaration',
    atlas_cell: 'cellule atlas',
    entity_identification: 'identification',
    link: 'lien dérivé',
    absence: 'absence constatée',
    causal_claim: 'affirmation causale (de la source)',
  };
  let what = '';
  if (e.kind === 'kg_statement') {
    const when = [x.point_in_time, x.start && `depuis ${String(x.start)}`, x.end && `jusqu’en ${String(x.end)}`].filter(Boolean).join(', ');
    what = `${labelOf(x.subject)} — ${labelOf(x.property)} — ${labelOf(x.object) || (typeof x.value === 'string' ? x.value : '')}${when ? ` (${when})` : ''}`;
  } else if (e.kind === 'entity_identification') {
    what = `${labelOf(x)} (${String(x.qid ?? '')})${x.query ? ` pour « ${String(x.query)} »` : ''}`;
  } else if (e.kind === 'atlas_cell') {
    const v = typeof x.value === 'string' ? x.value : typeof x.value_text === 'string' ? x.value_text : 'manquant';
    what = `${String(x.concept_id ?? '')} · ${String(x.iso3 ?? '')} · ${String(x.period_start ?? '').slice(0, 4)} : ${v} ${String(x.unit ?? '')}`;
  } else if (e.kind === 'absence') {
    what = `rien trouvé (${String(x.what ?? '')}) · sources consultées : ${Array.isArray(x.sources_consulted) ? (x.sources_consulted as unknown[]).map(String).join(', ') : 'non précisées'}`;
  } else if (e.kind === 'link') {
    what = `lien ${String(x.link_kind ?? '')} ${String(x.a ?? '')} – ${String(x.b ?? '')}`;
  } else if (e.kind === 'causal_claim') {
    // a claim made by a named source (never REVA's): source, tier, grade
    what = `${labelOf(x.cause)} → ${labelOf(x.effect)} · ${String(x.relation ?? '')}${x.stance === 'denies' ? ' (lien nié)' : ''} · selon ${source} (${s('tier') ?? '?'}) · ${
      typeof x.grade === 'string' ? `preuve ${x.grade}` : 'non gradée'
    }`;
  }
  return (
    <li id={`ev-${e.id}`} className={`v3-ev${active ? ' active' : ''}`} data-testid="evidence-item">
      <strong>{e.id}</strong> <span className="v3-chip">{kindFr[e.kind] ?? e.kind}</span> {what && <span>{what}</span>}{' '}
      <span className="v3-ev-src">
        {href ? (
          <a href={href} target="_blank" rel="noreferrer noopener">
            {wd ? 'Wikidata' : source} {stmt ?? (typeof x.qid === 'string' ? x.qid : '')}
          </a>
        ) : (
          <span>
            {source} {stmt ?? ''}
          </span>
        )}
        {rev && wd && wikidataRevisionUrl(rev) && (
          <>
            {' '}
            ·{' '}
            <a href={wikidataRevisionUrl(rev)!} target="_blank" rel="noreferrer noopener">
              révision {rev}
            </a>
          </>
        )}
        {e.kind === 'atlas_cell' && (
          <span className="muted">
            {' '}
            {s('dataset')} · {s('indicator')} · sha256 {(s('raw_sha256') ?? '').slice(0, 12)}… {s('selector') ?? ''}
          </span>
        )}
      </span>
      {community && <CommunityBadge />}
      {x.origin === 'test_fixture' && <FixtureBadge />}
    </li>
  );
}
