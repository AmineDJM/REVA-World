import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { v8Links } from '../../contract';
import { EmptyState, ErrorState, KeyHint, Skeleton } from '../../shell/ui';
import { AnswerCard } from './AnswerCard';
import { askAnswer, brainstorm, examples, type AnswerBundle, type BrainstormBundle, type Examples } from './api';
import { BrainstormView } from './BrainstormView';
import { clearHistory, loadHistory, remember, type HistoryEntry } from './history';
import { brainstormHref } from './links';
import './demander.css';

const FALLBACK_EXAMPLES: Examples = {
  questions: ['Causes de décès en France', 'Quelle est la prévision d’inflation pour la France ?', 'Que se passe-t-il si la BCE monte ses taux de 50 pb ?', 'Que mange le loup ?'],
  brainstorms: ['prévoir l’inflation française à 4 trimestres', 'prévoir le prix du Brent'],
};

type Load<T> = { state: 'idle' } | { state: 'loading'; key: string } | { state: 'ok'; key: string; data: T } | { state: 'error'; key: string; error: unknown };

function useFetch<T>(key: string | null, fn: (k: string, s: AbortSignal) => Promise<T>): [Load<T>, () => void] {
  const [st, setSt] = useState<Load<T>>({ state: 'idle' });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (!key) {
      setSt({ state: 'idle' });
      return;
    }
    const ac = new AbortController();
    setSt({ state: 'loading', key });
    fn(key, ac.signal).then(
      (data) => !ac.signal.aborted && setSt({ state: 'ok', key, data }),
      (error) => !ac.signal.aborted && setSt({ state: 'error', key, error }),
    );
    return () => ac.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce]);
  return [st, () => setNonce((n) => n + 1)];
}

export default function DemanderPage() {
  const [params, setParams] = useSearchParams();
  const mode: 'question' | 'brainstorm' = params.get('mode') === 'brainstorm' ? 'brainstorm' : 'question';
  const q = mode === 'question' ? (params.get('q') ?? '').trim() : '';
  const t = mode === 'brainstorm' ? (params.get('t') ?? params.get('q') ?? '').trim() : '';
  const [draft, setDraft] = useState(mode === 'question' ? q : t);
  const [history, setHistory] = useState<HistoryEntry[]>(() => loadHistory());
  const [ex, setEx] = useState<Examples>(FALLBACK_EXAMPLES);
  const input = useRef<HTMLTextAreaElement>(null);

  const [answer, reloadAnswer] = useFetch<AnswerBundle>(q || null, askAnswer);
  const [brain, reloadBrain] = useFetch<BrainstormBundle>(t || null, brainstorm);

  useEffect(() => {
    setDraft(mode === 'question' ? q : t);
  }, [q, t, mode]);

  useEffect(() => {
    document.title = q ? `${q.slice(0, 60)} · Demander` : t ? `${t.slice(0, 60)} · Brainstormer` : 'Demander';
  }, [q, t]);

  useEffect(() => {
    const ac = new AbortController();
    examples(ac.signal).then(
      (e) => !ac.signal.aborted && e.questions.length > 0 && setEx(e),
      () => undefined,
    );
    return () => ac.abort();
  }, []);

  useEffect(() => {
    if (answer.state === 'ok') {
      const b = answer.data;
      const summary = b.headline.kind === 'number' && b.headline.value ? `${b.headline.value}${b.headline.unit ? ` ${b.headline.unit}` : ''}` : b.headline.text.slice(0, 120);
      setHistory(remember({ q: b.question, mode: 'question', status: b.status, summary }));
    }
  }, [answer]);
  useEffect(() => {
    if (brain.state === 'ok') setHistory(remember({ q: brain.data.target.target, mode: 'brainstorm', status: null, summary: `${brain.data.drivers.length} pilote(s), ${brain.data.next_steps.length} étape(s)` }));
  }, [brain]);

  const go = useCallback(
    (text: string, m: 'question' | 'brainstorm' = mode) => {
      const v = text.trim();
      if (!v) return;
      setParams(m === 'brainstorm' ? { mode: 'brainstorm', t: v } : { q: v }, { replace: false });
    },
    [mode, setParams],
  );

  const setMode = (m: 'question' | 'brainstorm') => {
    const v = draft.trim();
    setParams(m === 'brainstorm' ? (v ? { mode: 'brainstorm', t: v } : { mode: 'brainstorm' }) : v ? { q: v } : {}, { replace: false });
  };

  const onPin = useCallback(async (b: AnswerBundle): Promise<string | null> => {
    const { pinItem } = await import('../tableaux/pin');
    const pin = b.pin as Record<string, unknown>;
    const note = { kind: 'note' as const, label: String(pin.label ?? b.question).slice(0, 400), markdown: String(pin.markdown ?? '') };
    const r = await pinItem(note);
    const series = pin.series as Record<string, string> | undefined;
    if (series && series.concept && series.geo) {
      await pinItem({ kind: 'series', concept: series.concept, geo: series.geo, label: series.label ?? b.question, unit: series.unit || undefined });
    }
    return r.added ? `Épinglé dans « ${r.boardTitle} »${r.origin === 'local' ? ' (ce navigateur)' : ''}` : `Déjà dans « ${r.boardTitle} »`;
  }, []);

  const busy = (mode === 'question' && answer.state === 'loading') || (mode === 'brainstorm' && brain.state === 'loading');

  return (
    <div className="v8-page v8d">
      <header className="v8-page-head">
        <h1 className="v8-page-title">Demander</h1>
        <p className="v8-page-lede">Une question en français ou en anglais : REVA répond seulement avec ses données chargées, chaque phrase citée — ou dit ce qui manque.</p>
      </header>

      <div className="v8d-modes" role="tablist" aria-label="Mode">
        <button type="button" role="tab" aria-selected={mode === 'question'} className={`v8d-mode${mode === 'question' ? ' is-on' : ''}`} onClick={() => setMode('question')}>
          Question
        </button>
        <button type="button" role="tab" aria-selected={mode === 'brainstorm'} className={`v8d-mode${mode === 'brainstorm' ? ' is-on' : ''}`} onClick={() => setMode('brainstorm')}>
          Brainstormer
        </button>
      </div>

      <form
        className="v8d-ask"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          go(draft);
        }}
      >
        <label className="v8-sr-only" htmlFor="v8d-input">
          {mode === 'brainstorm' ? 'Cible à prévoir' : 'Question'}
        </label>
        <textarea
          id="v8d-input"
          ref={input}
          className="v8-input v8d-input"
          rows={1}
          value={draft}
          autoFocus={!q && !t}
          placeholder={mode === 'brainstorm' ? 'prévoir l’inflation française à 4 trimestres' : 'Quelle est la prévision d’inflation pour la France ?'}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              go(draft);
            } else if (e.key === 'ArrowUp' && !draft && history[0]) {
              e.preventDefault();
              setDraft(history[0].q);
            } else if (e.key === 'Escape') {
              (e.target as HTMLTextAreaElement).blur();
            }
          }}
          maxLength={2048}
        />
        <button type="submit" className="v8-btn v8-btn-primary" disabled={!draft.trim() || busy}>
          {mode === 'brainstorm' ? 'Brainstormer' : 'Demander'}
        </button>
        <span className="v8d-hint v8-subtle">
          <KeyHint keys="Enter" /> envoyer · <KeyHint keys="↑" /> question précédente · <KeyHint keys="Mod K" /> puis « ? » depuis partout
        </span>
      </form>

      <div className="v8d-layout">
        <main className="v8d-main">
          {mode === 'question' && (
            <>
              {answer.state === 'idle' && (
                <EmptyState title="Que voulez-vous savoir ?">
                  <span className="v8d-examples">
                    {ex.questions.map((x) => (
                      <button key={x} type="button" className="v8d-chip" onClick={() => go(x, 'question')}>
                        {x}
                      </button>
                    ))}
                  </span>
                </EmptyState>
              )}
              {answer.state === 'loading' && (
                <div className="v8d-loading" aria-busy="true">
                  <p className="v8-subtle v8d-pulse">REVA lit ses sources…</p>
                  <Skeleton height={88} />
                  <Skeleton lines={4} />
                </div>
              )}
              {answer.state === 'error' && <ErrorState error={answer.error} what="la réponse" onRetry={reloadAnswer} />}
              {answer.state === 'ok' && <AnswerCard key={answer.key} bundle={answer.data} onAsk={(x) => go(x, 'question')} onPin={() => onPin(answer.data)} />}
            </>
          )}
          {mode === 'brainstorm' && (
            <>
              {brain.state === 'idle' && (
                <EmptyState title="Que voulez-vous prévoir ?">
                  REVA liste depuis ses registres les pilotes attribués, les séries chargées ou manquantes, les prévisions existantes et leur jugement, puis les prochaines étapes.
                  <span className="v8d-examples">
                    {ex.brainstorms.map((x) => (
                      <button key={x} type="button" className="v8d-chip" onClick={() => go(x, 'brainstorm')}>
                        {x}
                      </button>
                    ))}
                  </span>
                </EmptyState>
              )}
              {brain.state === 'loading' && (
                <div className="v8d-loading" aria-busy="true">
                  <p className="v8-subtle v8d-pulse">REVA parcourt ses registres…</p>
                  <Skeleton lines={6} />
                </div>
              )}
              {brain.state === 'error' && <ErrorState error={brain.error} what="le brainstorm" onRetry={reloadBrain} />}
              {brain.state === 'ok' && <BrainstormView b={brain.data} />}
            </>
          )}
        </main>

        <aside className="v8d-history" aria-label="Historique">
          <div className="v8d-history-head">
            <h2 className="v8d-h3">Historique</h2>
            {history.length > 0 && (
              <button
                type="button"
                className="v8-btn v8-btn-sm v8-btn-ghost"
                onClick={() => {
                  clearHistory();
                  setHistory([]);
                }}
              >
                Effacer
              </button>
            )}
          </div>
          {history.length === 0 ? (
            <p className="v8-subtle">Vos questions de ce navigateur apparaîtront ici.</p>
          ) : (
            <ul className="v8d-history-list">
              {history.map((h) => (
                <li key={`${h.mode}:${h.q}`}>
                  <Link to={h.mode === 'brainstorm' ? brainstormHref(h.q) : v8Links.ask(h.q)} className={`v8d-hist${h.q === (mode === 'question' ? q : t) ? ' is-current' : ''}`}>
                    <span className={`v8d-dot v8d-dot-${h.status ?? 'brainstorm'}`} aria-hidden="true" />
                    <span className="v8d-hist-q">{h.mode === 'brainstorm' ? `Brainstormer : ${h.q}` : h.q}</span>
                    {h.summary && <span className="v8d-hist-s v8-subtle">{h.summary}</span>}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </aside>
      </div>
    </div>
  );
}
