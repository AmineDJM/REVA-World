import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { ApiError } from '../../../api/problem';
import { EmptyState, ErrorState, KeyHint, Skeleton } from '../../shell/ui';
import { fetchFamilies, fetchNeighbourhood, fetchPaths, type EgoData, type FamilyInfo, type NodeRef, type PathsData } from './api';
import { EXAMPLES } from './examples';
import { HOP_CHOICES, answerSentence, buildGraph, parseState, plural, stateQuery, warningsFr, type LiensState } from './model';
import { NodePicker, type PickedNode } from './NodePicker';
import { EgoList, NodeCard, PathCard } from './parts';
import './liens.css';

const PathGraph = lazy(() => import('./PathGraph'));

type Load<T> = { status: 'idle' } | { status: 'loading' } | { status: 'building' } | { status: 'error'; error: unknown } | { status: 'ok'; data: T; warnings: string[] };

const isBuilding = (e: unknown) => e instanceof ApiError && e.status === 503 && e.code === 'paths_index_building';

function typing(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
}

/** /v8/liens — how anything connects to anything (V8LIENS). State: ?from=&to=&fam=&causal=1&hops=&p=&n= */
export default function LiensPage() {
  const [params, setParams] = useSearchParams();
  const st = useMemo(() => parseState(params), [params]);
  const set = useCallback(
    (patch: Partial<LiensState>, push = false) => {
      const next = { ...parseState(params), ...patch };
      setParams(stateQuery(next), { replace: !push });
    },
    [params, setParams],
  );

  const [labels, setLabels] = useState<Record<string, string>>({});
  const [families, setFamilies] = useState<FamilyInfo[] | null>(null);
  const [paths, setPaths] = useState<Load<PathsData>>({ status: 'idle' });
  const [ego, setEgo] = useState<Load<EgoData>>({ status: 'idle' });
  const [expansions, setExpansions] = useState<{ center: string; data: EgoData }[]>([]);
  const [expanding, setExpanding] = useState('');
  const [retry, setRetry] = useState(0);
  const fromRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    document.title = 'Liens · REVA';
    fetchFamilies()
      .then(setFamilies)
      .catch(() => setFamilies([]));
  }, []);

  // paths between from and to
  const famKey = st.families.join(',');
  useEffect(() => {
    if (!st.from || !st.to) {
      setPaths({ status: 'idle' });
      return;
    }
    const ctrl = new AbortController();
    setPaths((p) => (p.status === 'building' ? p : { status: 'loading' }));
    setExpansions([]);
    fetchPaths({ from: st.from, to: st.to, families: st.families, causal: st.causal, hops: st.hops }, ctrl.signal)
      .then((r) => {
        setPaths({ status: 'ok', data: r.data, warnings: warningsFr(r.warnings, r.data) });
        setLabels((l) => ({ ...l, [st.from]: r.data.from.label, [st.to]: r.data.to.label }));
      })
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return;
        if (isBuilding(e)) {
          setPaths({ status: 'building' });
          setTimeout(() => setRetry((x) => x + 1), 4000);
        } else setPaths({ status: 'error', error: e });
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [st.from, st.to, famKey, st.causal, st.hops, retry]);

  // exploration mode: only one end chosen → its ego graph
  const centre = st.from && !st.to ? st.from : !st.from && st.to ? st.to : '';
  useEffect(() => {
    if (!centre) {
      setEgo({ status: 'idle' });
      return;
    }
    const ctrl = new AbortController();
    setEgo({ status: 'loading' });
    fetchNeighbourhood(centre, { families: st.families, causal: true, limit: 24 }, ctrl.signal)
      .then((r) => {
        setEgo({ status: 'ok', data: r.data, warnings: [] });
        setLabels((l) => ({ ...l, [centre]: r.data.center.label }));
      })
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return;
        if (isBuilding(e)) {
          setEgo({ status: 'building' });
          setTimeout(() => setRetry((x) => x + 1), 4000);
        } else setEgo({ status: 'error', error: e });
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [centre, famKey, retry]);

  const data = paths.status === 'ok' ? paths.data : null;
  const egoData = ego.status === 'ok' ? ego.data : null;
  const allNodes: Record<string, NodeRef> = useMemo(() => {
    const out: Record<string, NodeRef> = {};
    if (egoData) Object.assign(out, egoData.nodes);
    for (const x of expansions) Object.assign(out, x.data.nodes);
    if (data) Object.assign(out, data.nodes);
    return out;
  }, [data, egoData, expansions]);
  const graph = useMemo(
    () => buildGraph(data, egoData ? [{ center: egoData.center.id, data: egoData }, ...expansions] : expansions),
    [data, egoData, expansions],
  );
  const pathCount = data?.paths.length ?? 0;
  const selPath = Math.min(st.path, Math.max(0, pathCount - 1));
  const selNode = st.node && allNodes[st.node] ? allNodes[st.node] : null;

  const expand = useCallback(
    (id: string) => {
      if (!id || expansions.some((x) => x.center === id) || expanding) return;
      setExpanding(id);
      fetchNeighbourhood(id, { families: st.families, causal: st.causal || !data, limit: 14 })
        .then((r) => setExpansions((xs) => [...xs, { center: id, data: r.data }]))
        .catch(() => undefined)
        .finally(() => setExpanding(''));
    },
    [expansions, expanding, st.families, st.causal, data],
  );

  const pick = useCallback((id: string) => set({ node: id }), [set]);
  const swap = useCallback(() => set({ from: st.to, to: st.from, path: 0, node: '' }, true), [set, st.from, st.to]);

  // keyboard: / focus · j k paths · x swap · c causal · e expand · f fiche
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || typing(e.target)) return;
      if (e.key === '/') {
        e.preventDefault();
        fromRef.current?.focus();
      } else if (e.key === 'j' && pathCount) set({ path: Math.min(pathCount - 1, selPath + 1) });
      else if (e.key === 'k' && pathCount) set({ path: Math.max(0, selPath - 1) });
      else if (e.key === 'x' && (st.from || st.to)) swap();
      else if (e.key === 'c') set({ causal: !st.causal, path: 0 }, true);
      else if (e.key === 'e' && st.node) expand(st.node);
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pathCount, selPath, set, swap, st.from, st.to, st.causal, st.node, expand]);

  const fromVal: PickedNode | null = st.from ? { id: st.from, label: labels[st.from] ?? st.from } : null;
  const toVal: PickedNode | null = st.to ? { id: st.to, label: labels[st.to] ?? st.to } : null;
  const toggleFamily = (f: string) => {
    const has = st.families.includes(f);
    set({ families: has ? st.families.filter((x) => x !== f) : [...st.families, f], path: 0 }, true);
  };
  const note = data?.note.fr ?? null;

  return (
    <div className="v8-page lien-page">
      <header className="v8-page-head">
        <h1 className="v8-page-title">Liens</h1>
        <p className="v8-page-lede">Comment deux choses sont-elles reliées ? Chaque lien cite sa source et sa licence.</p>
      </header>

      <section className="lien-query" aria-label="Requête">
        <div className="lien-ends">
          <NodePicker
            ref={fromRef}
            label="De"
            value={fromVal}
            placeholder="pays, molécule, gène, conflit, espèce, entreprise…"
            testId="lien-from"
            onChange={(v) => {
              if (v) setLabels((l) => ({ ...l, [v.id]: v.label }));
              set({ from: v?.id ?? '', path: 0, node: '' }, true);
            }}
          />
          <button type="button" className="v8-btn v8-btn-ghost lien-swap" onClick={swap} aria-label="Inverser départ et arrivée" title="Inverser (x)" disabled={!st.from && !st.to}>
            ⇄
          </button>
          <NodePicker
            label="À"
            value={toVal}
            placeholder="… ou un indicateur : « prix à la consommation »"
            testId="lien-to"
            onChange={(v) => {
              if (v) setLabels((l) => ({ ...l, [v.id]: v.label }));
              set({ to: v?.id ?? '', path: 0, node: '' }, true);
            }}
          />
        </div>
        <div className="lien-options">
          <label className="lien-toggle" title="Raccourci : c">
            <input type="checkbox" checked={st.causal} onChange={(e) => set({ causal: e.target.checked, path: 0 }, true)} data-testid="lien-causal" />
            <span>inclure les affirmations causales attribuées</span>
          </label>
          <label className="lien-hopsel">
            <span>liens max.</span>
            <select className="v8-input" value={st.hops} onChange={(e) => set({ hops: Number(e.target.value), path: 0 }, true)}>
              {HOP_CHOICES.map((h) => (
                <option key={h} value={h}>
                  {h}
                </option>
              ))}
            </select>
          </label>
          <span className="lien-keys" aria-hidden="true">
            <KeyHint keys="/" /> chercher <KeyHint keys="j" /> <KeyHint keys="k" /> chemins <KeyHint keys="x" /> inverser <KeyHint keys="c" /> causal
          </span>
        </div>
        <div className="lien-fams" role="group" aria-label="Familles de relations">
          <button type="button" className={`lien-chip${st.families.length === 0 ? ' is-on' : ''}`} aria-pressed={st.families.length === 0} onClick={() => set({ families: [], path: 0 }, true)}>
            toutes les familles
          </button>
          {(families ?? []).map((f) => (
            <button key={f.id} type="button" className={`lien-chip${st.families.includes(f.id) ? ' is-on' : ''}`} aria-pressed={st.families.includes(f.id)} onClick={() => toggleFamily(f.id)} title={f.id}>
              {f.label}
            </button>
          ))}
        </div>
        <div className="lien-honesty" data-testid="lien-honesty">
          <strong>Un chemin est un ensemble de liens documentés, pas une preuve causale.</strong> Les maillons causaux sont des affirmations de leurs sources (palier T1–T4, note A–E) ; les estimations sont celles de leurs auteurs.
          {note && (
            <details>
              <summary>en détail</summary>
              {note}
            </details>
          )}
        </div>
      </section>

      {!st.from && !st.to && (
        <EmptyState title="Choisissez deux entités, ou essayez un exemple" icon="⟿">
          <p>Les liens viennent des familles de relations du graphe de connaissances (situé dans, produit, fait partie de…) ; avec l’option causale, des affirmations causales attribuées et des estimations d’effet publiées.</p>
          <ul className="lien-examples">
            {EXAMPLES.map((x) => (
              <li key={x.label}>
                <button type="button" className="lien-example" onClick={() => setParams(stateQuery({ ...parseState(new URLSearchParams()), from: x.from, to: x.to, causal: x.causal ?? false, hops: x.hops ?? 4 }), { replace: false })}>
                  <span className="lien-example-domain">{x.domain}</span>
                  <span>{x.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </EmptyState>
      )}

      {(paths.status === 'building' || ego.status === 'building') && (
        <div className="lien-building" role="status">
          Index des liens en construction sur le serveur (environ une minute la première fois) — nouvel essai automatique…
        </div>
      )}

      {st.from && st.to && paths.status === 'error' && <ErrorState error={paths.error} what="la recherche de chemins" onRetry={() => setRetry((x) => x + 1)} />}
      {centre && ego.status === 'error' && <ErrorState error={ego.error} what="les voisins" onRetry={() => setRetry((x) => x + 1)} />}

      {(data || egoData || paths.status === 'loading' || ego.status === 'loading') && (
        <div className="lien-results">
          <section className="lien-col-list" aria-label="Chemins">
            {paths.status === 'loading' && <Skeleton lines={8} label="Recherche des chemins…" />}
            {data && (
              <>
                <p className="lien-answer" data-testid="lien-answer">
                  {answerSentence(data)}
                </p>
                {paths.status === 'ok' && paths.warnings.length > 0 && (
                  <ul className="lien-warn">
                    {paths.warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                )}
                {!data.found && (
                  <EmptyState title="Aucun chemin dans ces limites" compact>
                    <p>{data.not_found_reason}</p>
                    <p>
                      {!st.causal && (
                        <button type="button" className="v8-btn v8-btn-sm" onClick={() => set({ causal: true }, true)}>
                          inclure les affirmations causales
                        </button>
                      )}{' '}
                      {st.hops < 6 && (
                        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={() => set({ hops: Math.min(6, st.hops + 1) }, true)}>
                          autoriser {st.hops + 1} liens
                        </button>
                      )}
                    </p>
                  </EmptyState>
                )}
                {data.paths.map((p, i) => (
                  <PathCard key={p.rank} p={p} d={data} selected={i === selPath} onSelect={() => set({ path: i })} onPick={pick} />
                ))}
                <SearchFooter d={data} />
              </>
            )}
            {!data && ego.status === 'loading' && <Skeleton lines={8} label="Voisins…" />}
            {!data && egoData && (
              <>
                <p className="lien-answer">
                  Voisins de <strong>{egoData.center.label}</strong> : {plural(egoData.edges.length, 'lien affiché', 'liens affichés')} sur {Object.values(egoData.groups).reduce((a, b) => a + b, 0).toLocaleString('fr-FR')}. Choisissez l’autre bout pour chercher un chemin.
                </p>
                <EgoList data={egoData} onPick={pick} />
              </>
            )}
          </section>
          <section className="lien-col-graph" aria-label="Graphe">
            <Suspense fallback={<Skeleton height={460} />}>
              {graph.nodes.size > 0 ? <PathGraph model={graph} selectedPath={selPath} selectedNode={st.node} onTapNode={pick} /> : <Skeleton height={460} />}
            </Suspense>
            <p className="lien-legend" aria-hidden="true">
              <span className="lg lg-doc" /> lien documenté <span className="lg lg-claim" /> affirmation causale <span className="lg lg-est" /> estimation <span className="lg lg-decl" /> lien déclaré
            </p>
            {selNode && (
              <NodeCard
                node={selNode}
                expanded={expansions.some((x) => x.center === selNode.id) || egoData?.center.id === selNode.id}
                busy={expanding === selNode.id}
                onExpand={() => expand(selNode.id)}
                onFrom={() => set({ from: selNode.id, node: '', path: 0 }, true)}
                onTo={() => set({ to: selNode.id, node: '', path: 0 }, true)}
              />
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function SearchFooter({ d }: { d: PathsData }) {
  const s = d.search;
  const idx = d.index as Record<string, unknown>;
  const n = (k: string) => (typeof idx[k] === 'number' ? (idx[k] as number).toLocaleString('fr-FR') : '?');
  return (
    <footer className="lien-foot" data-testid="lien-foot">
      <p>
        Recherche : {plural(s.entries_scanned, 'lien examiné', 'liens examinés')}, {plural(s.nodes_reached, 'nœud atteint', 'nœuds atteints')}, {Math.round(s.search_ms)} ms · {s.max_hops} liens au plus
        {s.families?.length ? ` · familles : ${s.families.join(', ')}` : ''} · index : {n('statements')} déclarations, {n('causal_claims')} affirmations causales, {n('effect_estimates')} estimations
        {typeof idx.built_at === 'string' ? ` (construit ${idx.built_at.replace('T', ' ').replace('Z', ' UTC')})` : ''}.
      </p>
      {s.hubs_not_expanded_count > 0 && (
        <p>
          Nœuds très connectés atteints mais non traversés ({s.hubs_not_expanded_count}) : {s.hubs_not_expanded.slice(0, 6).map((h) => h.label).join(', ')}
          {s.hubs_not_expanded_count > 6 ? '…' : ''}
        </p>
      )}
      {s.truncated && <p className="lien-warn-line">Recherche bornée : {s.truncation_reasons.join(' ; ')} — un chemin peut exister au-delà.</p>}
    </footer>
  );
}
