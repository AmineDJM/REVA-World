import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { useVirtualizer } from '@tanstack/react-virtual';
import { entityLabel, getKgEntity, getKgNeighbours, type KgEntity } from '../api/kg';
import { useApi } from '../../lib/useApi';
import { CitationChip } from '../components/CitationChip';
import { EntityCard } from '../components/EntityCard';
import { EntityPicker, type PickedEntity } from '../components/EntityPicker';
import { KindGlyph, KindLegend } from '../components/KindLegend';
import { V3Empty, V3Error, V3Loading } from '../components/States';
import { citeKgStatement } from '../lib/citations';
import { emptyGraph, incidentEdges, mergeNeighbours, setNodeKind, visibleAt, yearRange, type GEdge, type GraphModel } from '../lib/graph';
import { kindGroup, type KindGroup } from '../lib/kinds';
import { formatYear } from '../lib/time';
import type { FreshBurst } from '../components/NeuronGraph';

const NeuronGraph = lazy(() => import('../components/NeuronGraph'));

export const MAX_NODES = 2500;
const KIND_CONCURRENCY = 4;
const NOW = new Date().getUTCFullYear();

type NodeLoad = { status: 'loading' } | { status: 'error'; error: unknown };

export default function NeuronesPage() {
  const [params, setParams] = useSearchParams();
  const root = params.get('qid');
  const yearParam = params.get('annee');
  const [model, setModel] = useState<GraphModel>(emptyGraph);
  const modelRef = useRef(model);
  const [selected, setSelected] = useState<string | null>(root);
  const [burst, setBurst] = useState<FreshBurst | null>(null);
  const [loads, setLoads] = useState<Record<string, NodeLoad>>({});
  const [layoutSeq, setLayoutSeq] = useState(0);
  const [fitSeq, setFitSeq] = useState(0);
  const [includeUndated, setIncludeUndated] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);
  const [nodeFilter, setNodeFilter] = useState('');
  const [resetSeq, setResetSeq] = useState(0);
  const generation = useRef(0);
  const burstSeq = useRef(0);
  const kindCache = useRef(new Map<string, KgEntity | null>());
  const kindQueue = useRef<string[]>([]);
  const kindActive = useRef(0);

  const year = yearParam !== null && /^-?\d+$/.test(yearParam) ? Number(yearParam) : null;

  const commit = useCallback((m: GraphModel) => {
    modelRef.current = m;
    setModel(m);
  }, []);

  const pumpKinds = useCallback(() => {
    const gen = generation.current;
    while (kindActive.current < KIND_CONCURRENCY && kindQueue.current.length) {
      const id = kindQueue.current.shift()!;
      if (kindCache.current.has(id)) continue;
      kindActive.current++;
      getKgEntity(id)
        .then((r) => {
          kindCache.current.set(id, r.data);
          if (gen === generation.current) commit(setNodeKind(modelRef.current, id, r.data.kind ?? null, entityLabel(r.data, undefined) || undefined));
        })
        .catch(() => kindCache.current.set(id, null))
        .finally(() => {
          kindActive.current--;
          if (gen === generation.current) pumpKinds();
        });
    }
  }, [commit]);

  const expand = useCallback(
    async (qid: string, more = false) => {
      const m = modelRef.current;
      const node = m.nodes.get(qid);
      if (!more && node?.expanded) return;
      if (m.nodes.size >= MAX_NODES) {
        setNotice(`Limite d’affichage atteinte (${MAX_NODES} nœuds). Repartez d’une entité ou réinitialisez.`);
        return;
      }
      const gen = generation.current;
      setLoads((l) => ({ ...l, [qid]: { status: 'loading' } }));
      try {
        const r = await getKgNeighbours(qid, { limit: 80, cursor: more ? (node?.nextCursor ?? undefined) : undefined });
        if (gen !== generation.current) return;
        const res = mergeNeighbours(modelRef.current, { qid, label: node?.label && node.label !== qid ? node.label : r.data.label }, r.data.edges, r.next_cursor ?? null);
        let g = res.graph;
        for (const id of res.addedNodes) {
          const known = kindCache.current.get(id);
          if (known) g = setNodeKind(g, id, known.kind ?? null, entityLabel(known, undefined) || undefined);
        }
        commit(g);
        burstSeq.current += 1;
        setBurst({ seq: burstSeq.current, parent: qid, nodes: res.addedNodes.filter((n) => n !== qid) });
        setLoads((l) => {
          const { [qid]: _, ...rest } = l;
          return rest;
        });
        kindQueue.current.push(...res.addedNodes.filter((n) => !kindCache.current.has(n)));
        pumpKinds();
        if (res.addedNodes.length === 0 && r.data.edges.length === 0) setNotice(`Aucun voisin (relation entre entités) chargé pour ${r.data.label}.`);
        else setNotice(null);
      } catch (error) {
        if (gen !== generation.current) return;
        setLoads((l) => ({ ...l, [qid]: { status: 'error', error } }));
      }
    },
    [commit, pumpKinds],
  );

  // (Re)start from the root entity.
  useEffect(() => {
    generation.current += 1;
    kindQueue.current = [];
    commit(emptyGraph());
    setLoads({});
    setSelected(root);
    setNotice(null);
    if (!root) return;
    const gen = generation.current;
    if (!kindCache.current.has(root)) {
      getKgEntity(root)
        .then((r) => {
          kindCache.current.set(root, r.data);
          if (gen === generation.current) commit(setNodeKind(modelRef.current, root, r.data.kind ?? null, entityLabel(r.data, undefined) || undefined));
        })
        .catch(() => undefined);
    }
    void expand(root);
  }, [root, resetSeq, commit, expand]);

  const visibility = useMemo(() => visibleAt(model, year, includeUndated, root), [model, year, includeUndated, root]);
  const range = useMemo(() => yearRange(model) ?? ([1400, NOW] as [number, number]), [model]);
  const counts = useMemo(() => {
    const c: Partial<Record<KindGroup, number>> = {};
    for (const n of model.nodes.values()) {
      const g = kindGroup(n.kind);
      c[g] = (c[g] ?? 0) + 1;
    }
    return c;
  }, [model]);

  const setParam = (k: string, v: string | null) => {
    const p = new URLSearchParams(params);
    if (v === null || v === '') p.delete(k);
    else p.set(k, v);
    setParams(p, { replace: k === 'annee' });
  };

  const onPick = (e: PickedEntity | null) => {
    if (e) setParam('qid', e.qid);
  };

  const onTapNode = useCallback(
    (id: string) => {
      setSelected(id);
      void expand(id);
    },
    [expand],
  );

  const rootLoad = root ? loads[root] : undefined;
  const rootFailed = root && rootLoad?.status === 'error' && model.edges.size === 0;
  const selectedNode = selected ? model.nodes.get(selected) : undefined;
  const selLoad = selected ? loads[selected] : undefined;

  const listed = useMemo(() => {
    const f = nodeFilter.trim().toLowerCase();
    return [...model.nodes.values()].filter((n) => !f || n.label.toLowerCase().includes(f) || n.id.toLowerCase().includes(f)).sort((a, b) => a.order - b.order);
  }, [model, nodeFilter]);

  return (
    <section className="v3-page v3-neurones" aria-labelledby="neurones-title">
      <header className="v3-page-head">
        <h1 id="neurones-title">Neurones — le monde relié</h1>
        <p className="v3-lede">
          Partez de n’importe quelle entité (pays, empire, personne, événement, langue…) et dépliez ses connexions. Chaque lien est une déclaration
          sourcée ; cliquez un nœud pour l’ouvrir et le déplier.
        </p>
      </header>
      <div className="v3-toolbar">
        <EntityPicker label="Point de départ" value={root ? { qid: root, label: model.nodes.get(root)?.label ?? root } : null} onChange={onPick} testId="neurones-picker" />
        <fieldset className="v3-year-filter">
          <legend>En vigueur en</legend>
          <label className="v3-check">
            <input type="checkbox" checked={year !== null} onChange={(e) => setParam('annee', e.target.checked ? String(Math.min(range[1], Math.max(range[0], 1500))) : null)} />
            filtrer par année
          </label>
          <input
            type="range"
            min={range[0]}
            max={Math.max(range[1], range[0] + 1)}
            value={year ?? range[1]}
            disabled={year === null}
            aria-label="Année"
            aria-valuetext={year === null ? 'aucun filtre' : formatYear(year)}
            onChange={(e) => setParam('annee', e.target.value)}
          />
          <output className="v3-year-out" aria-live="polite">
            {year === null ? 'toutes périodes' : formatYear(year)}
          </output>
          <label className="v3-check">
            <input type="checkbox" checked={includeUndated} onChange={(e) => setIncludeUndated(e.target.checked)} />
            inclure les liens non datés
          </label>
        </fieldset>
        <div className="v3-toolbar-actions">
          <button type="button" onClick={() => setLayoutSeq((s) => s + 1)} disabled={model.nodes.size < 2}>
            Disposer (force)
          </button>
          <button type="button" onClick={() => setFitSeq((s) => s + 1)} disabled={model.nodes.size === 0}>
            Recentrer
          </button>
          <button
            type="button"
            onClick={() => setResetSeq((n) => n + 1)}
            disabled={!root}
          >
            Réinitialiser
          </button>
        </div>
      </div>
      {!root && (
        <V3Empty>
          <p>
            <strong>Choisissez une entité pour commencer.</strong> Par exemple : Christophe Colomb, le traité de Tordesillas, le Brésil, la langue
            portugaise, l’Empire ottoman…
          </p>
        </V3Empty>
      )}
      {rootFailed && rootLoad?.status === 'error' && <V3Error error={rootLoad.error} what="Le graphe de connaissances" onRetry={() => root && void expand(root, true)} />}
      {notice && (
        <p className="v3-notice" role="status">
          {notice}
        </p>
      )}
      {root && !rootFailed && (
        <div className="v3-neuron-layout">
          <div className="v3-neuron-main">
            <div className="v3-neuron-stage">
              <Suspense fallback={<V3Loading what="du moteur de graphe" />}>
                <NeuronGraph
                  model={model}
                  visibility={visibility}
                  root={root}
                  selected={selected}
                  burst={burst}
                  layoutSeq={layoutSeq}
                  fitSeq={fitSeq}
                  onTapNode={onTapNode}
                  label={`Graphe de ${model.nodes.size} entités et ${model.edges.size} déclarations autour de ${model.nodes.get(root)?.label ?? root}. La liste des nœuds sous le graphe donne le même contenu au clavier.`}
                />
              </Suspense>
              {rootLoad?.status === 'loading' && model.nodes.size === 0 && <div className="v3-stage-overlay"><V3Loading what="des voisins" /></div>}
              <p className="v3-stage-stats" aria-live="polite">
                {model.nodes.size} nœuds · {model.edges.size} liens{year !== null ? ` · ${visibility.edges.size} en vigueur en ${formatYear(year)}` : ''}
              </p>
            </div>
            <KindLegend counts={counts} />
            <p className="field-hint">
              Pointillés : lien non daté dans la source (ce n’est pas « toujours »). Tirets orangés : relation causale <em>affirmée par la source</em>{' '}
              (a pour cause / a pour effet), jamais déduite par REVA.
            </p>
            <details className="v3-node-list" open={model.nodes.size > 0 && model.nodes.size < 12}>
              <summary>Liste des nœuds ({model.nodes.size}) — navigation au clavier</summary>
              <label className="v3-inline">
                Filtrer <input type="search" value={nodeFilter} onChange={(e) => setNodeFilter(e.target.value)} />
              </label>
              <ul>
                {listed.slice(0, 300).map((n) => (
                  <li key={n.id}>
                    <button type="button" className={`link-button${n.id === selected ? ' v3-current' : ''}`} onClick={() => onTapNode(n.id)} aria-pressed={n.id === selected}>
                      <KindGlyph group={kindGroup(n.kind)} /> {n.label}
                    </button>
                    <span className="muted"> {n.id}{n.expanded ? ' · déplié' : ''}{visibility.nodes.has(n.id) ? '' : ' · hors période'}</span>
                  </li>
                ))}
              </ul>
              {listed.length > 300 && <p className="muted">… {listed.length - 300} autres : affinez le filtre.</p>}
            </details>
          </div>
          <aside className="v3-side" aria-label="Entité sélectionnée">
            {selected ? (
              <SidePanel
                key={selected}
                qid={selected}
                model={model}
                visibleEdges={visibility.edges}
                year={year}
                expanded={!!selectedNode?.expanded}
                hasMore={!!selectedNode?.nextCursor}
                load={selLoad}
                onExpand={(more) => void expand(selected, more)}
                onSelect={(id) => {
                  onTapNode(id);
                  setFitSeq((s) => s + 1);
                }}
              />
            ) : (
              <p className="muted">Sélectionnez un nœud.</p>
            )}
          </aside>
        </div>
      )}
    </section>
  );
}

function SidePanel({
  qid,
  model,
  visibleEdges,
  year,
  expanded,
  hasMore,
  load,
  onExpand,
  onSelect,
}: {
  qid: string;
  model: GraphModel;
  visibleEdges: Set<string>;
  year: number | null;
  expanded: boolean;
  hasMore: boolean;
  load: NodeLoad | undefined;
  onExpand: (more: boolean) => void;
  onSelect: (id: string) => void;
}) {
  const ent = useApi((s) => getKgEntity(qid, s), [qid]);
  const edges = useMemo(() => incidentEdges(model, qid), [model, qid]);
  const label = model.nodes.get(qid)?.label ?? qid;
  return (
    <div className="v3-side-inner">
      {ent.status === 'loading' && <V3Loading what="de la fiche" />}
      {ent.status === 'error' && <V3Error error={ent.error} what={`La fiche de ${label}`} onRetry={ent.reload} />}
      {ent.status === 'ok' && <EntityCard entity={ent.data.data} fallbackLabel={label} />}
      <div className="v3-side-actions">
        {!expanded && (
          <button type="button" className="primary" onClick={() => onExpand(false)} disabled={load?.status === 'loading'}>
            Déplier les voisins
          </button>
        )}
        {expanded && hasMore && (
          <button type="button" onClick={() => onExpand(true)} disabled={load?.status === 'loading'}>
            Charger plus de voisins
          </button>
        )}
        {load?.status === 'loading' && <V3Loading what="des voisins" />}
      </div>
      {load?.status === 'error' && <V3Error error={load.error} what="Les voisins" onRetry={() => onExpand(expanded)} />}
      <h3>
        Déclarations affichées ({edges.length}){year !== null ? ` · en vigueur en ${formatYear(year)} : ${edges.filter((e) => visibleEdges.has(e.id)).length}` : ''}
      </h3>
      {edges.length === 0 ? (
        <p className="muted">{expanded ? 'Aucune relation entre entités chargée pour ce nœud.' : 'Dépliez le nœud pour voir ses déclarations.'}</p>
      ) : (
        <StatementList qid={qid} edges={edges} model={model} visibleEdges={visibleEdges} onSelect={onSelect} />
      )}
    </div>
  );
}

function StatementList({ qid, edges, model, visibleEdges, onSelect }: { qid: string; edges: GEdge[]; model: GraphModel; visibleEdges: Set<string>; onSelect: (id: string) => void }) {
  const parent = useRef<HTMLDivElement>(null);
  const v = useVirtualizer({ count: edges.length, getScrollElement: () => parent.current, estimateSize: () => 84, overscan: 6 });
  const items = v.getVirtualItems();
  // jsdom (tests) has no layout: render a bounded slice directly when the virtualizer yields nothing.
  const rows = items.length ? items.map((it) => ({ index: it.index, start: it.start, key: it.key })) : edges.slice(0, 40).map((_, i) => ({ index: i, start: -1, key: i }));
  return (
    <div ref={parent} className="v3-statements" role="list" aria-label="Déclarations et leurs citations">
      <div style={{ height: items.length ? v.getTotalSize() : undefined, position: 'relative' }}>
        {rows.map((r) => {
          const e = edges[r.index]!;
          const out = e.source === qid;
          const otherId = out ? e.target : e.source;
          const other = model.nodes.get(otherId)?.label ?? otherId;
          const inForce = visibleEdges.has(e.id);
          return (
            <div
              key={r.key}
              role="listitem"
              data-index={r.index}
              ref={items.length ? v.measureElement : undefined}
              className={`v3-statement${inForce ? '' : ' v3-out-of-period'}`}
              style={r.start >= 0 ? { position: 'absolute', top: 0, left: 0, right: 0, transform: `translateY(${r.start}px)` } : undefined}
            >
              <p className="v3-statement-text">
                {out ? (
                  <>
                    <span className="v3-prop">{e.label}</span> →{' '}
                    <button type="button" className="link-button" onClick={() => onSelect(otherId)}>
                      {other}
                    </button>
                  </>
                ) : (
                  <>
                    <button type="button" className="link-button" onClick={() => onSelect(otherId)}>
                      {other}
                    </button>{' '}
                    — <span className="v3-prop">{e.label}</span> → ici
                  </>
                )}
                {e.causal && <span className="v3-badge v3-badge-causal">cause affirmée par la source</span>}
                {(out ? otherId : qid).startsWith('atlas:') && (
                  <>
                    {' '}
                    <Link className="v3-atlas-link" to={`/v3/fiche?qid=${encodeURIComponent(out ? qid : otherId)}#fiche-series`}>
                      voir la série (Fiche)
                    </Link>
                  </>
                )}
              </p>
              <p className="v3-statement-meta">
                <span className={e.temporal.kind === 'undated' ? 'v3-undated' : ''}>{e.dates}</span>
                {!inForce && <span className="muted"> · hors période choisie</span>}
              </p>
              <CitationChip c={citeKgStatement(e.statement)} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
