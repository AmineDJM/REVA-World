import { useEffect, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router';
import { classifyV3Error } from '../api/common';
import { getKgEntity, getKgPath, entityLabel } from '../api/kg';
import { getLinksPath } from '../api/links';
import { CitationChip } from '../components/CitationChip';
import { EntityPicker, type PickedEntity } from '../components/EntityPicker';
import { PathNotCauseBanner } from '../components/Honesty';
import { V3Empty, V3Error, V3Loading } from '../components/States';
import { fromKg, fromLinks, reasonFr, type PathsView, type PathView } from '../lib/paths';
import { prefersReducedMotion } from '../../lib/useApi';

type Engine = 'links' | 'kg';
type Result =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ok'; view: PathsView; warnings: string[]; fellBack: boolean }
  | { status: 'error'; error: unknown };

function intParam(v: string | null, def: number, lo: number, hi: number): number {
  const n = v !== null && /^-?\d+$/.test(v) ? Number(v) : def;
  return Math.min(hi, Math.max(lo, n));
}

export default function CheminPage() {
  const [params, setParams] = useSearchParams();
  const from = params.get('from');
  const to = params.get('to');
  const hops = intParam(params.get('etapes'), 4, 1, 6);
  const k = intParam(params.get('k'), 3, 1, 10);
  const year = params.get('annee') && /^-?\d+$/.test(params.get('annee')!) ? Number(params.get('annee')) : null;
  const engine: Engine = params.get('moteur') === 'kg' ? 'kg' : 'links';
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [result, setResult] = useState<Result>({ status: 'idle' });
  const [draft, setDraft] = useState({ hops, k, year: year === null ? '' : String(year), engine });

  useEffect(() => setDraft({ hops, k, year: year === null ? '' : String(year), engine }), [hops, k, year, engine]);

  // Resolve labels of ids given in the URL (deep links, palette).
  useEffect(() => {
    for (const id of [from, to]) {
      if (!id || labels[id] || !/^Q\d+$/.test(id)) continue;
      getKgEntity(id)
        .then((r) => setLabels((l) => ({ ...l, [id]: entityLabel(r.data, id) })))
        .catch(() => undefined);
    }
  }, [from, to, labels]);

  useEffect(() => {
    if (!from || !to) {
      setResult({ status: 'idle' });
      return;
    }
    const ctrl = new AbortController();
    setResult({ status: 'loading' });
    const viaKg = () =>
      getKgPath({ from, to, max_hops: hops, max_paths: k, ...(year !== null ? { valid_from: String(year), valid_to: String(year) } : {}) }, ctrl.signal).then((r) => ({
        view: fromKg(r.data),
        warnings: r.warnings.map((w) => w.message),
      }));
    const viaLinks = () =>
      getLinksPath({ from, to, k, max_hops: hops, ...(year !== null ? { as_of: year } : {}) }, ctrl.signal).then((r) => ({
        view: fromLinks(r.data),
        warnings: r.warnings.map((w) => w.message),
      }));
    const run = async () => {
      if (engine === 'kg') return { ...(await viaKg()), fellBack: false };
      try {
        return { ...(await viaLinks()), fellBack: false };
      } catch (e) {
        // The LINKS engine is not deployed yet: use the raw KG path search (same honesty rules).
        if (classifyV3Error(e).kind === 'unavailable' && /^Q\d+$/.test(from) && /^Q\d+$/.test(to)) return { ...(await viaKg()), fellBack: true };
        throw e;
      }
    };
    run()
      .then((r) => !ctrl.signal.aborted && setResult({ status: 'ok', ...r }))
      .catch((error: unknown) => {
        if (!ctrl.signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) setResult({ status: 'error', error });
      });
    return () => ctrl.abort();
  }, [from, to, hops, k, year, engine]);

  const set = (patch: Record<string, string | null>) => {
    const p = new URLSearchParams(params);
    for (const [key, v] of Object.entries(patch)) {
      if (v === null || v === '') p.delete(key);
      else p.set(key, v);
    }
    setParams(p);
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    set({ etapes: String(draft.hops), k: String(draft.k), annee: draft.year.trim() || null, moteur: draft.engine === 'kg' ? 'kg' : null });
  };

  const pick = (key: 'from' | 'to') => (e: PickedEntity | null) => {
    if (e) setLabels((l) => ({ ...l, [e.qid]: e.label }));
    set({ [key]: e ? e.qid : null });
  };

  return (
    <section className="v3-page v3-chemin" aria-labelledby="chemin-title">
      <header className="v3-page-head">
        <h1 id="chemin-title">Chemin — comment X est-il lié à Y ?</h1>
        <p className="v3-lede">Trouvez les chaînes de déclarations sourcées qui relient deux entités (ex. Christophe Colomb → … → Brésil → portugais).</p>
      </header>
      <PathNotCauseBanner />
      <form className="v3-path-form" onSubmit={submit} aria-label="Paramètres du chemin">
        <EntityPicker label="De (X)" value={from ? { qid: from, label: labels[from] ?? from } : null} onChange={pick('from')} allowIso3 testId="path-from" />
        <EntityPicker label="À (Y)" value={to ? { qid: to, label: labels[to] ?? to } : null} onChange={pick('to')} allowIso3 testId="path-to" />
        <label>
          Étapes max.
          <select value={draft.hops} onChange={(e) => setDraft((d) => ({ ...d, hops: Number(e.target.value) }))}>
            {[1, 2, 3, 4, 5, 6].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label>
          Chemins
          <select value={draft.k} onChange={(e) => setDraft((d) => ({ ...d, k: Number(e.target.value) }))}>
            {[1, 2, 3, 5, 8, 10].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label>
          En vigueur en (année)
          <input type="number" inputMode="numeric" value={draft.year} placeholder="toutes" onChange={(e) => setDraft((d) => ({ ...d, year: e.target.value }))} min={-5000} max={3000} />
        </label>
        <label>
          Moteur
          <select value={draft.engine} onChange={(e) => setDraft((d) => ({ ...d, engine: e.target.value as Engine }))}>
            <option value="links">liens typés</option>
            <option value="kg">graphe brut</option>
          </select>
        </label>
        <button type="submit" className="primary">
          Appliquer
        </button>
        {from && to && (
          <button type="button" onClick={() => set({ from: to, to: from })}>
            Inverser X ⇄ Y
          </button>
        )}
      </form>
      {result.status === 'idle' && (
        <V3Empty>
          <p>
            <strong>Choisissez deux entités.</strong> Identifiants Wikidata (Q…) ou codes pays ISO3 acceptés.
          </p>
        </V3Empty>
      )}
      {result.status === 'loading' && <V3Loading what="des chemins" />}
      {result.status === 'error' && <V3Error error={result.error} what="La recherche de chemin" />}
      {result.status === 'ok' && <PathsResult view={result.view} warnings={result.warnings} fellBack={result.fellBack} />}
    </section>
  );
}

export function PathsResult({ view, warnings, fellBack }: { view: PathsView; warnings: string[]; fellBack: boolean }) {
  return (
    <div className="v3-paths" data-testid="paths-result">
      <p className="v3-meta">
        Moteur : {view.engine === 'links' ? 'liens typés (LINKS)' : 'graphe brut (KG)'}
        {fellBack && ' — le moteur de liens typés n’est pas encore disponible, résultat du graphe brut'}
        {view.truncated && ' · recherche bornée : un lien peut exister au-delà'}
      </p>
      {view.disclaimer && <p className="field-hint">{view.disclaimer}</p>}
      {warnings.length > 0 && (
        <ul className="v3-warnings" aria-label="Avertissements du serveur">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {!view.found || view.paths.length === 0 ? (
        <V3Empty>
          <p>
            <strong>Aucun chemin.</strong> {reasonFr(view.reason)}.
          </p>
        </V3Empty>
      ) : (
        <ol className="v3-path-list">
          {view.paths.map((p, i) => (
            <li key={i}>
              <PathChain path={p} index={i} />
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function PathChain({ path, index }: { path: PathView; index: number }) {
  const animate = !prefersReducedMotion();
  return (
    <article className={`v3-chain${animate ? ' v3-animate' : ''}`} aria-label={`Chemin ${index + 1} : ${path.hops.length} étape(s)`} data-testid="path-chain">
      <h3>
        Chemin {index + 1} <span className="muted">· {path.hops.length} étape{path.hops.length > 1 ? 's' : ''}</span>
      </h3>
      <ol className="v3-chain-nodes" aria-hidden="true">
        {path.nodes.map((n, i) => (
          <li key={`${n.id}-${i}`} style={{ animationDelay: `${i * 160}ms` }}>
            <span className="v3-chain-node">{n.label}</span>
          </li>
        ))}
      </ol>
      <ol className="v3-hops">
        {path.hops.map((h, i) => (
          <li key={i} className={`v3-hop${h.causal ? ' v3-hop-causal' : ''}`} style={{ animationDelay: `${i * 160 + 80}ms` }} data-testid="path-hop">
            <p className="v3-hop-sentence">
              {h.causal ? (
                <span className="v3-hop-rel v3-causal-text" data-testid="causal-wording">
                  {h.sentence}
                </span>
              ) : (
                <>
                  <Link to={`/v3/neurones?qid=${encodeURIComponent(h.from.id)}`}>{h.from.label}</Link> <span className="v3-hop-rel">{h.sentence}</span>{' '}
                  <Link to={`/v3/neurones?qid=${encodeURIComponent(h.to.id)}`}>{h.to.label}</Link>
                </>
              )}
            </p>
            <p className="v3-hop-meta">
              <span className={h.undated ? 'v3-undated' : ''}>{h.dates || 'dates non précisées'}</span>
              {h.via.length > 0 && <span> · via {h.via.join(', ')}</span>}
              {h.causal && <span className="v3-badge v3-badge-causal">affirmé par {h.causal.assertedBy}</span>}
            </p>
            <div className="v3-hop-cites">
              {h.citations.length ? h.citations.map((c, j) => <CitationChip key={j} c={c} />) : <span className="v3-missing">aucune citation fournie par le serveur</span>}
            </div>
            {h.serverText && (
              <details className="v3-hop-server">
                <summary>Formulation du serveur</summary>
                <p>{h.serverText}</p>
              </details>
            )}
          </li>
        ))}
      </ol>
    </article>
  );
}
