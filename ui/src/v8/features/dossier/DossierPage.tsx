import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Link, useLocation, useNavigate } from 'react-router';
import { EmptyState, ErrorState, Skeleton } from '../../shell/ui';
import { dossierHref, exportUrl, getDossier, getSubjects, KIND_LABEL, type DossierDoc, type SubjectHit } from './api';
import { Reader } from './Reader';
import { linkNote, PinButton } from '../tableaux/PinButton';
import { useLoad } from './useLoad';
import './dossier.css';

/** /v8/dossier[/<kind>/<id>] → [kind, id] (ids may contain ':' and are URL-encoded). */
export function parseDossierPath(pathname: string): [string, string] | null {
  const m = /^\/v8\/dossier\/([^/]+)\/([^/]+)\/?$/.exec(pathname);
  if (!m) return null;
  try {
    return [decodeURIComponent(m[1]!), decodeURIComponent(m[2]!)];
  } catch {
    return null;
  }
}

export default function DossierPage() {
  const { pathname } = useLocation();
  const sel = parseDossierPath(pathname);
  useEffect(() => {
    document.title = sel ? `${sel[1]} · Dossier` : 'Dossier';
  }, [sel?.[0], sel?.[1]]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="v8-page dos-page">
      {sel ? <Brief kind={sel[0]} id={sel[1]} /> : <Picker />}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- subject picker
function Picker() {
  const featured = useLoad('featured', () => getSubjects(null, null));
  const [q, setQ] = useState('');
  const [kind, setKind] = useState<string | null>(null);
  const [debounced, setDebounced] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 160);
    return () => clearTimeout(t);
  }, [q]);
  const results = useLoad(debounced.length >= 2 ? `q:${kind ?? ''}:${debounced}` : null, () => getSubjects(debounced, kind));
  const navigate = useNavigate();
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === '/' && document.activeElement?.tagName !== 'INPUT' && document.activeElement?.tagName !== 'TEXTAREA') {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  const kinds = featured.data?.kinds ?? [];
  const featuredHits = (featured.data?.hits ?? []).filter((h) => !kind || h.kind === kind);
  const hits: SubjectHit[] = debounced.length >= 2 ? (results.data?.hits ?? []) : featuredHits;
  const go = (h: SubjectHit) => navigate(dossierHref(h.kind, h.id));
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => Math.min(hits.length - 1, a + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === 'Enter' && hits[active]) {
      e.preventDefault();
      go(hits[active]!);
    }
  };
  return (
    <>
      <header className="v8-page-head">
        <h1 className="v8-page-title">Dossier</h1>
        <p className="v8-page-lede">
          Un dossier lisible en un clic — pays, matière première, maladie, médicament, conflit, décision — où chaque nombre porte sa source, sa licence et sa
          période, et où ce qui manque est écrit.
        </p>
      </header>
      <div className="dos-picker">
        <div className="dos-search">
          <input
            ref={input}
            className="v8-input dos-search-input"
            type="search"
            placeholder="Chercher un sujet : France, paludisme, imatinib, Ukraine, OPEP…"
            aria-label="Chercher un sujet de dossier"
            aria-controls="dos-hits"
            aria-activedescendant={hits[active] ? `dos-hit-${active}` : undefined}
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setActive(0);
            }}
            onKeyDown={onKey}
            autoFocus
          />
          <span className="dos-key" aria-hidden>
            /
          </span>
        </div>
        <div className="dos-kinds" role="group" aria-label="Type de dossier">
          <button type="button" className={`dos-kind-btn${kind === null ? ' is-on' : ''}`} aria-pressed={kind === null} onClick={() => setKind(null)}>
            Tous
          </button>
          {kinds.map((k) => (
            <button key={k.kind} type="button" className={`dos-kind-btn${kind === k.kind ? ' is-on' : ''}`} aria-pressed={kind === k.kind} onClick={() => setKind(k.kind)}>
              {k.label}
            </button>
          ))}
        </div>
        {featured.status === 'error' && <ErrorState error={featured.error} what="la liste des dossiers" onRetry={featured.reload} />}
        {debounced.length < 2 && featured.status === 'loading' && <Skeleton lines={4} />}
        <h2 className="dos-hits-title">{debounced.length >= 2 ? `Résultats pour « ${debounced} »` : 'Dossiers prêts'}</h2>
        {debounced.length >= 2 && results.status === 'ok' && hits.length === 0 && (
          <EmptyState title="Aucun sujet trouvé">Essayez un nom de pays, un code ISO3, une maladie, un médicament ou un conflit UCDP.</EmptyState>
        )}
        <ul id="dos-hits" className="dos-hits" role="listbox" aria-label="Sujets">
          {hits.map((h, i) => (
            <li key={`${h.kind}:${h.id}`} id={`dos-hit-${i}`} role="option" aria-selected={i === active} className={i === active ? 'is-active' : undefined}>
              <Link to={dossierHref(h.kind, h.id)} className="dos-hit" onMouseEnter={() => setActive(i)}>
                <span className="dos-hit-kind">{KIND_LABEL[h.kind] ?? h.kind}</span>
                <span className="dos-hit-label">{h.label}</span>
                {h.sublabel && <span className="dos-hit-sub">{h.sublabel}</span>}
              </Link>
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------------------------- one brief
function Brief({ kind, id }: { kind: string; id: string }) {
  const [refresh, setRefresh] = useState(0);
  const load = useCallback(() => getDossier(kind, id, { refresh: refresh > 0 }), [kind, id, refresh]);
  const doc = useLoad(`${kind}/${id}/${refresh}`, load);
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    setSlow(false);
    if (doc.status !== 'loading') return;
    const t = setTimeout(() => setSlow(true), 2500);
    return () => clearTimeout(t);
  }, [doc.status]);
  return (
    <div className="dos-layout">
      <aside className="dos-side" aria-label="Dossier : sommaire et exports">
        <Link to="/v8/dossier" className="dos-back">
          ← Autres dossiers
        </Link>
        {doc.data && <Toc doc={doc.data} />}
        <div className="dos-exports" role="group" aria-label="Télécharger">
          <a className="v8-btn v8-btn-sm" href={exportUrl(kind, id, 'html')} target="_blank" rel="noreferrer">
            Version imprimable (PDF)
          </a>
          <a className="v8-btn v8-btn-sm" href={exportUrl(kind, id, 'md')} download={`dossier-${kind}-${id.replace(/:/g, '_')}.md`}>
            Markdown
          </a>
          <a className="v8-btn v8-btn-sm" href={exportUrl(kind, id, 'json')} download={`dossier-${kind}-${id.replace(/:/g, '_')}.json`}>
            JSON
          </a>
          {doc.data && (
            <PinButton
              title="Épingler ce dossier sur un tableau (référence ; le dossier est recalculé à l’ouverture)"
              item={() => (doc.data ? linkNote(`Dossier : ${doc.data.title}`, dossierHref(kind, id), doc.data.subtitle ? [doc.data.subtitle] : []) : null)}
            />
          )}
          {doc.data && (
            <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={() => setRefresh((r) => r + 1)} title="Recalculer même si une version en cache existe">
              Recalculer
            </button>
          )}
        </div>
        {doc.data && (
          <p className="dos-side-meta">
            Modèle {doc.data.meta.template} v{doc.data.meta.template_version}
            <br />
            {doc.data.meta.cached ? 'servi depuis le cache' : `calculé en ${(doc.data.meta.compute_ms / 1000).toFixed(1).replace('.', ',')} s`}
          </p>
        )}
      </aside>
      <div className="dos-main">
        {doc.status === 'loading' && !doc.data && (
          <div className="dos-loading" role="status">
            <Skeleton height={48} />
            <Skeleton lines={6} />
            <p className="dos-muted">
              {slow
                ? 'Première génération de ce dossier : les sources sont lues et chaque valeur est rattachée à sa note. Les fois suivantes, le dossier est servi depuis le cache.'
                : 'Chargement du dossier…'}
            </p>
          </div>
        )}
        {doc.status === 'error' && <ErrorState error={doc.error} what="ce dossier" onRetry={doc.reload} />}
        {doc.data && <Reader doc={doc.data} />}
      </div>
    </div>
  );
}

function Toc({ doc }: { doc: DossierDoc }) {
  const items = useMemo(
    () => [
      ...(doc.summary.length ? [{ id: 'dos-essentiel', title: 'L’essentiel' }] : []),
      ...doc.sections.map((s) => ({ id: `dos-s-${s.id}`, title: s.title, missing: s.blocks.filter((b) => b.type === 'missing').length })),
      { id: 'dos-s-non-couvert', title: 'Ce que ce dossier ne couvre pas', missing: doc.not_covered.length },
      { id: 'dos-s-sources', title: `Sources (${doc.footnotes.length})` },
    ],
    [doc],
  );
  return (
    <details className="dos-toc" open={typeof window === 'undefined' || !window.matchMedia || window.matchMedia('(min-width: 1040px)').matches}>
      <summary>Sommaire</summary>
      <ol>
        {items.map((it) => (
          <li key={it.id}>
            <a href={`#${it.id}`}>
              {it.title}
              {'missing' in it && it.missing ? <span className="dos-toc-miss" title={`${it.missing} manque(s) déclaré(s)`}>{it.missing}</span> : null}
            </a>
          </li>
        ))}
      </ol>
    </details>
  );
}
