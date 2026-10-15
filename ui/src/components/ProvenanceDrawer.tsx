import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { getProvenance } from '../api/client';
import type { ProvenanceGraph, ProvenanceKind, ProvenanceNode } from '../api/types';
import { useApi } from '../lib/useApi';
import { humanize, isRecord } from '../lib/format';
import { ErrorState, Loading } from './StateViews';
import { ContextBanner } from './ContextBanner';

export const PROVENANCE_KINDS: readonly ProvenanceKind[] = [
  'estimate',
  'observation',
  'acquisition',
  'artifact',
  'sim_result',
  'release',
];

interface Target {
  kind: string;
  id: string;
  label?: string;
}

const ProvenanceCtx = createContext<(t: Target) => void>(() => undefined);

export function useProvenance() {
  return useContext(ProvenanceCtx);
}

/** Button that opens the provenance drawer for a record. */
export function ProvenanceButton({ kind, id, label }: { kind: string; id: string | null | undefined; label?: string }) {
  const open = useProvenance();
  if (!id) return <span className="muted">no provenance link</span>;
  return (
    <button type="button" className="link-button" onClick={() => open({ kind, id, label })}>
      {label ?? 'Provenance'}
    </button>
  );
}

export function ProvenanceProvider({ children }: { children: ReactNode }) {
  const [stack, setStack] = useState<Target[]>([]);
  const lastFocus = useRef<HTMLElement | null>(null);
  const open = useCallback((t: Target) => {
    lastFocus.current = document.activeElement as HTMLElement | null;
    setStack([t]);
  }, []);
  const close = useCallback(() => {
    setStack([]);
    lastFocus.current?.focus?.();
  }, []);
  const push = useCallback((t: Target) => setStack((s) => [...s, t]), []);
  const back = useCallback(() => setStack((s) => s.slice(0, -1)), []);
  const current = stack[stack.length - 1];
  return (
    <ProvenanceCtx.Provider value={open}>
      {children}
      {current && <ProvenanceDrawer target={current} depth={stack.length} onClose={close} onFollow={push} onBack={back} />}
    </ProvenanceCtx.Provider>
  );
}

const SELECTOR_KEYS = ['raw_selector', 'selector', 'json_pointer', 'pointer', 'locator', 'raw_field', 'field_path'];
const SHA_KEYS = ['sha256', 'raw_sha256', 'artifact_sha256'];

function pickAttr(attrs: Record<string, unknown>, keys: string[]): unknown {
  for (const k of keys) if (attrs[k] !== undefined && attrs[k] !== null) return attrs[k];
  return undefined;
}

/** "artifact:abc…" -> "abc…" (API node ids are kind-prefixed). */
export function nodeIdent(n: { id: string; kind: string }): string {
  return n.id.startsWith(`${n.kind}:`) ? n.id.slice(n.kind.length + 1) : n.id;
}

function NodeDetails({ node, selectors }: { node: ProvenanceNode; selectors: { relation: string; selector: unknown }[] }) {
  const attrs = isRecord(node.attributes) ? node.attributes : {};
  const sha = pickAttr(attrs, SHA_KEYS) ?? (node.kind === 'artifact' ? nodeIdent(node) : undefined);
  const selector = pickAttr(attrs, SELECTOR_KEYS);
  const pinned = typeof attrs.dataset_artifacts_pinned === 'number' ? attrs.dataset_artifacts_pinned : null;
  const rest = Object.entries(attrs).filter(([k]) => !SHA_KEYS.includes(k) && !SELECTOR_KEYS.includes(k) && k !== 'dataset_artifacts_pinned');
  return (
    <dl className="kv compact">
      {sha !== undefined && (
        <div className="kv-row">
          <dt>SHA-256</dt>
          <dd className="mono break">{String(sha)}</dd>
        </div>
      )}
      {selector !== undefined && (
        <div className="kv-row">
          <dt>Raw selector</dt>
          <dd className="mono break">{typeof selector === 'string' ? selector : JSON.stringify(selector)}</dd>
        </div>
      )}
      {selectors.map((sel, i) => (
        <div className="kv-row" key={`sel-${i}`}>
          <dt>Selector ({humanize(sel.relation)})</dt>
          <dd className="mono break">{typeof sel.selector === 'string' ? sel.selector : JSON.stringify(sel.selector)}</dd>
        </div>
      ))}
      {pinned !== null && (
        <div className="kv-row" data-testid="dataset-artifacts-pinned">
          <dt>Dataset inputs pinned</dt>
          <dd>
            {pinned} dataset artifact{pinned === 1 ? '' : 's'} (pinned by this release for model runs; not inputs of this value, not expanded here)
          </dd>
        </div>
      )}
      {rest.map(([k, v]) => (
        <div className="kv-row" key={k}>
          <dt>{humanize(k)}</dt>
          <dd className="break">{typeof v === 'object' && v !== null ? <code>{JSON.stringify(v)}</code> : String(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function ProvenanceDrawer({
  target,
  depth,
  onClose,
  onFollow,
  onBack,
}: {
  target: Target;
  depth: number;
  onClose: () => void;
  onFollow: (t: Target) => void;
  onBack: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const state = useApi((s) => getProvenance(target.kind, target.id, undefined, s), [target.kind, target.id]);

  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      if (e.key === 'Tab' && ref.current) {
        const f = ref.current.querySelectorAll<HTMLElement>('button, a[href], [tabindex]:not([tabindex="-1"])');
        if (f.length === 0) return;
        const first = f[0]!;
        const last = f[f.length - 1]!;
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, target]);

  const graph: ProvenanceGraph | undefined = state.data?.data;
  const ordered = useMemo(() => orderNodes(graph), [graph]);
  const selectorsByTarget = useMemo(() => {
    const m = new Map<string, { relation: string; selector: unknown }[]>();
    for (const e of graph?.edges ?? []) {
      const sel = e.attributes?.selector;
      if (sel === undefined || sel === null) continue;
      m.set(e.target, [...(m.get(e.target) ?? []), { relation: e.relation, selector: sel }]);
    }
    return m;
  }, [graph]);
  const nodeLabel = useMemo(() => new Map(graph?.nodes.map((n) => [n.id, `${humanize(n.kind)}: ${n.label}`]) ?? []), [graph]);

  return (
    <div className="drawer-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={ref}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="prov-title"
        tabIndex={-1}
      >
        <header className="drawer-header">
          <h2 id="prov-title">Provenance</h2>
          <p className="muted">
            {humanize(target.kind)} <span className="mono">{target.id}</span>
            {target.label ? ` — ${target.label}` : ''}
          </p>
          <div className="drawer-actions">
            {depth > 1 && (
              <button type="button" onClick={onBack}>
                Back
              </button>
            )}
            <button type="button" onClick={onClose}>
              Close
            </button>
          </div>
        </header>
        {state.status === 'loading' && <Loading what="lineage" />}
        {state.status === 'error' && <ErrorState error={state.error} what="Lineage" onRetry={state.reload} />}
        {state.data && <ContextBanner meta={state.data} scope="Lineage" />}
        {graph && (
          <>
            {graph.truncated && (
              <p className="bound-flag" role="status">
                Lineage truncated at {graph.max_nodes} nodes. Follow a node to continue.
              </p>
            )}
            <ol className="prov-chain">
              {ordered.map((n) => (
                <li key={n.id} className={`prov-node prov-${n.kind}`}>
                  <p>
                    <span className="tag">{humanize(n.kind)}</span> <strong>{n.label}</strong>{' '}
                    <span className="mono muted" title={n.id}>
                      {n.id.length > 24 ? `${n.id.slice(0, 24)}…` : n.id}
                    </span>
                  </p>
                  <NodeDetails node={n} selectors={selectorsByTarget.get(n.id) ?? []} />
                  {n.id !== graph.root && (PROVENANCE_KINDS as readonly string[]).includes(n.kind) && (
                    <button type="button" className="link-button" onClick={() => onFollow({ kind: n.kind, id: nodeIdent(n), label: n.label })}>
                      Trace from this {humanize(n.kind)}
                    </button>
                  )}
                </li>
              ))}
            </ol>
            {graph.edges.length > 0 && (
              <details>
                <summary>Edges ({graph.edges.length})</summary>
                <ul className="prov-edges">
                  {graph.edges.map((e, i) => (
                    <li key={i}>
                      {nodeLabel.get(e.source) ?? e.source} <span className="muted">—{humanize(e.relation)}→</span>{' '}
                      {nodeLabel.get(e.target) ?? e.target}
                      {Object.keys(e.attributes ?? {}).length > 0 && (
                        <code className="edge-attrs"> {JSON.stringify(e.attributes)}</code>
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** Breadth-first order from the root so the chain reads value → … → raw artifact. */
function orderNodes(graph: ProvenanceGraph | undefined): ProvenanceNode[] {
  if (!graph) return [];
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const adj = new Map<string, string[]>();
  for (const e of graph.edges) {
    adj.set(e.source, [...(adj.get(e.source) ?? []), e.target]);
    adj.set(e.target, [...(adj.get(e.target) ?? []), e.source]);
  }
  const seen = new Set<string>();
  const out: ProvenanceNode[] = [];
  const queue = [graph.root];
  while (queue.length) {
    const id = queue.shift()!;
    if (seen.has(id)) continue;
    seen.add(id);
    const n = byId.get(id);
    if (n) out.push(n);
    for (const nb of adj.get(id) ?? []) if (!seen.has(nb)) queue.push(nb);
  }
  for (const n of graph.nodes) if (!seen.has(n.id)) out.push(n);
  return out;
}
