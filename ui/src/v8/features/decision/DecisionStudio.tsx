import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Badge, EmptyState, ErrorState, KeyHint, Skeleton, Tabs } from '../../shell/ui';
import { pinItem } from '../tableaux/pin';
import { fetchBrief, fetchComparison, fetchSensitivity, fetchTemplates, runScenario, type Templates } from './api';
import { Compare } from './Compare';
import { Composer } from './Composer';
import { Effects } from './Effects';
import { TAB_IDS, type TabId } from './model';
import { Propagation } from './Propagation';
import { Sensitivity } from './Sensitivity';
import { isEmpty, parseState, resolveScenario, stateRef, stateToParams, withDraws, type ScenarioFields } from './state';
import { useAsync } from './useAsync';
import { Why } from './Why';
import './decision.css';

let templatesPromise: Promise<Templates> | null = null;
/** Shared, not abortable (React strict mode mounts twice; an aborted shared request would never settle). */
export function loadTemplates(): Promise<Templates> {
  if (!templatesPromise) {
    templatesPromise = fetchTemplates();
    templatesPromise.catch(() => {
      templatesPromise = null;
    });
  }
  return templatesPromise;
}
/** Tests only. */
export function resetTemplates() {
  templatesPromise = null;
}

const TAB_LABEL: Record<TabId, string> = {
  effets: 'Effets',
  propagation: 'Propagation',
  sensibilite: 'Sensibilité',
  comparer: 'Comparer A / B',
  pourquoi: 'Pourquoi / manques',
  brief: 'Brief',
};

function download(name: string, text: string) {
  try {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch {
    /* no download support (tests) */
  }
}

function typing(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
}

export default function DecisionStudio() {
  const [params, setParams] = useSearchParams();
  const [tpl] = useAsync('templates', () => loadTemplates());
  const st = useMemo(() => parseState(params), [params]);
  const t = tpl.status === 'ok' ? tpl.data : null;
  const ra = useMemo(() => (t ? resolveScenario(st.a, t) : null), [st.a, t]);
  const rb = useMemo(() => (t && st.b ? resolveScenario(st.b, t) : null), [st.b, t]);
  const bodyA = ra ? withDraws(ra.body, st) : null;
  const bodyB = rb ? withDraws(rb.body, st) : null;
  const keyA = bodyA ? JSON.stringify(bodyA) : null;
  const [run, retry] = useAsync(keyA, (s) => runScenario(bodyA!, s));
  const bundle = run.status === 'ok' ? run.data.bundle : null;

  const tab = (TAB_IDS as readonly string[]).includes(params.get('tab') ?? '') ? (params.get('tab') as TabId) : 'effets';
  const setView = useCallback(
    (kv: Record<string, string | null>, push = false) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(kv)) {
            if (v === null || v === '') next.delete(k);
            else next.set(k, v);
          }
          return next;
        },
        { replace: !push },
      ),
    [setParams],
  );

  const go = useCallback(
    (a: ScenarioFields, b: ScenarioFields | null = st.b) => {
      const next = stateToParams({ ...st, a, b }, params, false);
      if (b) next.set('tab', params.get('tab') === 'comparer' ? 'comparer' : (params.get('tab') ?? 'effets'));
      setParams(next, { replace: false });
    },
    [st, params, setParams],
  );

  const [sensKey, compKey, briefKey] = [
    tab === 'sensibilite' && bundle && keyA ? keyA : null,
    tab === 'comparer' && bodyA && bodyB ? JSON.stringify([bodyA, bodyB]) : null,
    tab === 'brief' && bundle && keyA ? keyA : null,
  ];
  const href = `/v8/decision?${params.toString()}`;
  const [sens] = useAsync(sensKey, (s) => fetchSensitivity(bodyA!, s));
  const [comp] = useAsync(compKey, (s) => fetchComparison(bodyA!, bodyB!, s));
  const [brief] = useAsync(briefKey, (s) => fetchBrief({ ...bodyA!, url: `/v8/decision?${stateToParams(st).toString()}` }, s));
  const [pinMsg, setPinMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    if (ra?.decision) document.title = `${ra.decision.label_fr.slice(0, 60)} · Décision`;
    else document.title = 'Décision';
  }, [ra?.decision]);

  // keyboard: 1–6 tabs, Shift+1–5 reference decisions, j/k next/previous variable
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typing(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
      const d = /^Digit([1-6])$/.exec(e.code);
      if (d && t) {
        const i = Number(d[1]) - 1;
        if (e.shiftKey) {
          const r = t.references[i];
          if (r) {
            e.preventDefault();
            go({ ref: r.id });
          }
        } else if (TAB_IDS[i]) {
          e.preventDefault();
          setView({ tab: TAB_IDS[i] === 'effets' ? null : TAB_IDS[i]! }, true);
        }
        return;
      }
      if ((e.key === 'j' || e.key === 'k') && bundle && bundle.headline.length) {
        const ids = bundle.headline.map((r) => r.id);
        const cur = Math.max(0, ids.indexOf(params.get('v') ?? ids[0]!));
        const nxt = ids[(cur + (e.key === 'j' ? 1 : ids.length - 1)) % ids.length]!;
        setView({ v: nxt });
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [t, bundle, params, go, setView]);

  if (tpl.status === 'error') return <ErrorState error={tpl.error} what="les modèles de décision" />;
  if (!t) return <Skeleton height={320} label="Chargement des décisions de référence" />;

  const pin = async () => {
    if (!ra?.decision) return;
    setBusy('pin');
    try {
      const r = await pinItem({ kind: 'decision', ref: stateRef(st.a), label: ra.decision.label_fr.slice(0, 400), summary: bundle?.diagnosis.headline_fr.slice(0, 2000) });
      setPinMsg(r.added ? `Épinglé sur « ${r.boardTitle} »${r.origin === 'local' ? ' (ce navigateur)' : ''}.` : `Déjà sur « ${r.boardTitle} ».`);
    } catch (err) {
      setPinMsg(`Épinglage impossible : ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  };
  const exportBrief = async () => {
    if (!bodyA) return;
    setBusy('brief');
    try {
      const b = await fetchBrief({ ...bodyA, url: `/v8/decision?${stateToParams(st).toString()}` });
      download(b.filename, b.markdown);
    } catch (err) {
      setPinMsg(`Export impossible : ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
    }
  };

  const warming = run.status === 'loading' && t.engine.state !== 'warm';
  const presets = t.compare_presets;

  return (
    <div className="v8-page v8d">
      <header className="v8-page-head">
        <h1 className="v8-page-title">Décision</h1>
        <p className="v8-page-lede">
          Que se passe-t-il si un acteur prend une décision ? Propagation par des estimations attribuées, chaque lien sourcé — un scénario conditionnel, jamais une prévision.
        </p>
      </header>

      <Composer templates={t} current={ra?.decision ?? null} base={ra?.variantOf ?? null} onRun={(f) => go(f)} />

      {ra && ra.problems.length > 0 && (
        <ul className="v8d-problems" role="alert">
          {ra.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}

      {isEmpty(st.a) && (
        <EmptyState title="Choisissez une décision" icon="⎇">
          Cinq décisions de référence ci-dessus (<KeyHint keys="Shift 1" /> … <KeyHint keys="Shift 5" />), ou composez la vôtre. Chaque scénario est une URL partageable.
        </EmptyState>
      )}

      {ra?.decision && (
        <section className="v8d-result" aria-label="Résultat du scénario">
          <header className="v8d-result-head">
            <div className="v8d-result-title">
              <h2>{ra.decision.label_fr}</h2>
              <div className="v8-row">
                <Badge kind="scenario" title={bundle?.honesty.label ?? t.label}>
                  {bundle?.honesty.validated ? 'chaîne validée par le juge' : 'scénario non validé — pas une probabilité'}
                </Badge>
                {ra.kind === 'variant' && <Badge kind="info">variante de référence</Badge>}
                {ra.kind === 'composed' && <Badge kind="info">décision composée</Badge>}
                {(bundle?.honesty.market || ra.instrument?.market) && <Badge kind="info" title={t.not_advice_fr}>pas un conseil financier</Badge>}
              </div>
            </div>
            <div className="v8d-actions v8-row">
              <button type="button" className="v8-btn v8-btn-sm" onClick={pin} disabled={busy !== null || !bundle}>
                Épingler
              </button>
              <button type="button" className="v8-btn v8-btn-sm" onClick={exportBrief} disabled={busy !== null || !bundle}>
                Exporter le brief (.md)
              </button>
              <button
                type="button"
                className="v8-btn v8-btn-sm v8-btn-ghost"
                onClick={() => {
                  void navigator.clipboard?.writeText(window.location.origin + href).then(() => setPinMsg('Lien copié.'), () => setPinMsg(href));
                }}
              >
                Copier le lien
              </button>
            </div>
          </header>
          {pinMsg && (
            <p className="v8d-small" role="status">
              {pinMsg}
            </p>
          )}

          {run.status === 'loading' && (
            <div>
              <Skeleton height={220} label="Propagation en cours" />
              {warming && <p className="v8-muted v8d-small">Premier calcul de ce processus serveur : préparation du moteur (index de l’atlas et du registre de prévisions), jusqu’à 2 minutes ; ensuite moins d’une seconde, puis en cache.</p>}
            </div>
          )}
          {run.status === 'error' && <ErrorState error={run.error} what="la propagation" onRetry={retry} />}
          {bundle && (
            <>
              <p className="v8d-answer v8d-answer-lg">{bundle.diagnosis.headline_fr}</p>
              <Tabs
                tabs={TAB_IDS.map((id) => ({
                  id,
                  label: TAB_LABEL[id],
                  count: id === 'effets' ? bundle.headline.length : id === 'pourquoi' ? bundle.diagnosis.blocking.length + bundle.diagnosis.plugins.length + bundle.diagnosis.ties.length || null : null,
                }))}
                value={tab}
                onChange={(id) => setView({ tab: id === 'effets' ? null : id }, true)}
                label="Vues du scénario"
                idBase="v8d"
                size="sm"
              />
              <div role="tabpanel" id={`v8d-panel-${tab}`} aria-labelledby={`v8d-tab-${tab}`} className="v8d-panel">
                {tab === 'effets' && (bundle.headline.length ? <Effects bundle={bundle} selected={params.get('v')} onSelect={(id) => setView({ v: id })} /> : <Why bundle={bundle} embedded />)}
                {tab === 'propagation' && <Propagation bundle={bundle} geo={params.get('g') ?? ''} onGeo={(g) => setView({ g })} edgeId={params.get('e')} onEdge={(id) => setView({ e: id })} />}
                {tab === 'sensibilite' &&
                  (sens.status === 'ok' ? (
                    <Sensitivity data={sens.data} variable={params.get('v')} period={params.get('p')} onVariable={(id) => setView({ v: id, p: null })} onPeriod={(p) => setView({ p })} />
                  ) : sens.status === 'error' ? (
                    <ErrorState error={sens.error} what="la sensibilité" />
                  ) : (
                    <Skeleton height={200} label="Calcul de la sensibilité" />
                  ))}
                {tab === 'comparer' && (
                  <div>
                    <div className="v8-row v8d-presets">
                      <span className="v8-muted v8d-small">Comparer :</span>
                      {presets.map((p) => (
                        <button
                          key={p.id}
                          type="button"
                          className="v8-btn v8-btn-sm"
                          onClick={() => {
                            const ref = t.references.find((r) => r.id === p.b_reference_id);
                            go({ ref: p.b_reference_id, m: p.a.magnitude.value }, ref ? { ref: ref.id } : null);
                          }}
                        >
                          {p.label_fr}
                        </button>
                      ))}
                      {!st.b && ra.decision && (
                        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={() => go(st.a, { ...st.a })}>
                          B = copie de A (modifiez A ensuite)
                        </button>
                      )}
                      {st.b && (
                        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={() => go(st.a, null)}>
                          Retirer B
                        </button>
                      )}
                    </div>
                    {!st.b ? (
                      <EmptyState title="Pas de scénario B" compact>
                        Choisissez un préréglage (p. ex. BCE +25 pb contre +50 pb) : A = le scénario affiché, B = l’autre. Les deux restent dans l’URL.
                      </EmptyState>
                    ) : rb && rb.problems.length ? (
                      <ul className="v8d-problems">
                        {rb.problems.map((p) => (
                          <li key={p}>B : {p}</li>
                        ))}
                      </ul>
                    ) : comp.status === 'ok' ? (
                      <Compare data={comp.data} />
                    ) : comp.status === 'error' ? (
                      <ErrorState error={comp.error} what="la comparaison" />
                    ) : (
                      <Skeleton height={200} label="Comparaison en cours" />
                    )}
                  </div>
                )}
                {tab === 'pourquoi' && <Why bundle={bundle} />}
                {tab === 'brief' &&
                  (brief.status === 'ok' ? (
                    <div className="v8d-brief">
                      <div className="v8-row">
                        <button type="button" className="v8-btn v8-btn-sm v8-btn-primary" onClick={() => download(brief.data.filename, brief.data.markdown)}>
                          Télécharger {brief.data.filename}
                        </button>
                        <button type="button" className="v8-btn v8-btn-sm" onClick={() => void navigator.clipboard?.writeText(brief.data.markdown)}>
                          Copier le Markdown
                        </button>
                        <span className="v8-muted v8d-small">{brief.data.n_citations} source(s) citée(s)</span>
                      </div>
                      <pre className="v8d-md" tabIndex={0}>
                        {brief.data.markdown}
                      </pre>
                    </div>
                  ) : brief.status === 'error' ? (
                    <ErrorState error={brief.error} what="le brief" />
                  ) : (
                    <Skeleton height={240} label="Rédaction du brief" />
                  ))}
              </div>
              <footer className="v8d-foot">
                <details>
                  <summary>Avertissements et provenance</summary>
                  <ul>
                    {bundle.honesty.disclaimers_fr.map((d) => (
                      <li key={d}>{d}</li>
                    ))}
                  </ul>
                  <p className="v8-mono v8d-small">
                    entrées {bundle.scenario_hash.slice(0, 12)} · chaîne {bundle.result.validation.chain_signature.slice(0, 12)} · {bundle.result.settings.draws} tirages, graine {bundle.result.settings.seed} · {bundle.cached ? 'en cache' : `${Math.round(bundle.compute_ms)} ms`}
                  </p>
                </details>
                <p className="v8-muted v8d-small">
                  <KeyHint keys="1" />–<KeyHint keys="6" /> vues · <KeyHint keys="j" />/<KeyHint keys="k" /> variable · <KeyHint keys="Shift 1" />–<KeyHint keys="Shift 5" /> décisions de référence
                </p>
              </footer>
            </>
          )}
        </section>
      )}
    </div>
  );
}
