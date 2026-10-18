/**
 * /v8/fiche/:kind/:id — the universal entity page (V8FICHE). ONE bundle call; answer first (name, kind, ids,
 * 3–6 headline numbers), then dense sections reachable by sticky in-page tabs and the keyboard:
 *   j / k  next / previous section      c  copy the citation      p  pin to a board (Tableaux)
 * Every value has its SourceChip; Missing is hatched with its reason; relations are not causes; causal claims and
 * effect estimates are attributed; forecasts carry their judged status.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router';
import { useApi } from '../../../lib/useApi';
import { v8Links } from '../../contract';
import { Badge, ErrorState, KeyHint, Skeleton, SourceChip } from '../../shell/ui';
import { getBundle, type Bundle } from './api';
import { chip, citation, sections, shortId, uniqueIds } from './model';
import { Causes, Facts, Forecasts, Headline, Missing, Relations, SeriesSection, Sources, SpaceTime } from './sections';
import './fiche.css';

export default function FichePage() {
  const params = useParams();
  const loc = useLocation();
  // params, else the path itself (a static route registered for the same URL carries no params)
  const m = /\/fiche\/([^/]+)\/([^/]+)/.exec(loc.pathname);
  const kind = params.kind ?? (m ? decodeURIComponent(m[1]!) : '');
  const id = params.id ?? (m ? decodeURIComponent(m[2]!) : '');
  const nav = useNavigate();
  const st = useApi((s) => getBundle(kind, id, s), [kind, id], !!kind && !!id);
  const b = st.data?.data ?? null;
  const fresh = st.status === 'ok' && b !== null;

  // canonical URL (drug/CHEMBL941 → drug/chembl:CHEMBL941), without a history entry
  useEffect(() => {
    if (!fresh || !b) return;
    const c = b.canonical;
    if (c.kind !== kind || c.id !== id) nav(v8Links.entity(c.kind, c.id) + window.location.search + window.location.hash, { replace: true });
  }, [fresh, b, kind, id, nav]);

  useEffect(() => {
    if (b?.identity) document.title = `${b.identity.label} · Fiche`;
  }, [b]);

  if (st.status === 'error' && !b) {
    return (
      <div className="v8-page fiche-page">
        <ErrorState error={st.error} what="la fiche" onRetry={st.reload} />
        <p className="v8-subtle">
          Demandé : <code>{kind}</code> / <code>{id}</code>. Une fiche accepte un code ISO3 pour un pays, un identifiant de source (<code>Q142</code>, <code>chebi:27732</code>, <code>hgnc:76</code>, <code>chembl:CHEMBL941</code>…) ou une page d’ancrage (<code>commodity/crude_oil</code>).
        </p>
      </div>
    );
  }
  if (!b) {
    return (
      <div className="v8-page fiche-page" aria-busy="true">
        <Skeleton height={36} width="40%" />
        <div className="fiche-stats">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} height={96} />
          ))}
        </div>
        <Skeleton lines={8} />
      </div>
    );
  }
  return <FicheView b={b} kind={b.canonical.kind} id={b.canonical.id} stale={st.status === 'loading'} />;
}

export function FicheView({ b, kind, id, stale = false }: { b: Bundle; kind: string; id: string; stale?: boolean }) {
  const secs = useMemo(() => sections(b), [b]);
  const [active, setActive] = useState(secs[0]?.id ?? 'faits');
  const [toast, setToast] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const ident = b.identity ? { ...b.identity, ids: uniqueIds(b.identity.ids) } : null;

  const flash = useCallback((m: string) => {
    setToast(m);
    window.setTimeout(() => setToast((t) => (t === m ? null : t)), 3200);
  }, []);

  const copyCitation = useCallback(async () => {
    const text = citation(b, window.location.origin + v8Links.entity(kind, id), new Date().toISOString().slice(0, 10));
    try {
      await navigator.clipboard.writeText(text);
      flash('Citation copiée.');
    } catch {
      flash('Copie impossible (presse-papiers refusé) : ' + text.slice(0, 80) + '…');
    }
  }, [b, kind, id, flash]);

  const pin = useCallback(async () => {
    try {
      const { pinItem } = await import('../tableaux/pin');
      const r = await pinItem({ kind: 'entity', entityKind: kind, id, label: ident?.label ?? id, sublabel: ident?.kind_label_fr ?? undefined });
      flash(r.added ? `Épinglé sur « ${r.boardTitle} »${r.origin === 'local' ? ' (dans ce navigateur)' : ''}.` : `Déjà sur « ${r.boardTitle} ».`);
    } catch (e) {
      flash(`Épinglage impossible : ${e instanceof Error ? e.message : String(e)}`);
    }
  }, [kind, id, ident, flash]);

  const goSection = useCallback(
    (sid: string) => {
      setActive(sid);
      const el = document.getElementById(`fiche-${sid}`);
      el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
      (el?.querySelector('h2') as HTMLElement | null)?.focus?.({ preventScroll: true });
    },
    [],
  );

  // keyboard: j/k sections, c citation, p pin (ignored while typing and after the shell's "g" prefix)
  useEffect(() => {
    let lastG = 0;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
      if (e.key === 'g') {
        lastG = Date.now();
        return;
      }
      if (Date.now() - lastG < 1500) return;
      const i = secs.findIndex((s) => s.id === active);
      if (e.key === 'j') {
        e.preventDefault();
        goSection(secs[Math.min(secs.length - 1, i + 1)]!.id);
      } else if (e.key === 'k') {
        e.preventDefault();
        goSection(secs[Math.max(0, i - 1)]!.id);
      } else if (e.key === 'c') {
        e.preventDefault();
        void copyCitation();
      } else if (e.key === 'p') {
        e.preventDefault();
        void pin();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [secs, active, goSection, copyCitation, pin]);

  // scroll-spy (where IntersectionObserver exists)
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(
      (entries) => {
        const vis = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        const first = vis[0];
        if (first) setActive(first.target.id.replace(/^fiche-/, ''));
      },
      { rootMargin: '-96px 0px -60% 0px', threshold: 0 },
    );
    for (const s of secs) {
      const el = document.getElementById(`fiche-${s.id}`);
      if (el) io.observe(el);
    }
    return () => io.disconnect();
  }, [secs]);

  const qid = ident?.qid ?? null;
  return (
    <div className={`v8-page fiche-page${stale ? ' fiche-stale' : ''}`} ref={rootRef} data-testid="fiche">
      <header className="fiche-head">
        <div className="fiche-kicker">
          <span className="fiche-kind">{ident?.kind_label_fr ?? kind}</span>
          {ident?.anchor && (
            <Badge kind="info" size="xs" title={ident.anchor_note ?? undefined}>
              page d’ancrage REVA
            </Badge>
          )}
          {ident?.community_edited && <Badge kind="community" size="xs" />}
          {(ident?.ids ?? []).slice(0, 6).map((i) => (
            <span key={`${i.scheme}|${i.value}`} className="fiche-id" title={i.label_fr ?? undefined}>
              <span className="fiche-id-s">{i.scheme}</span>{' '}
              {i.url ? (
                <a href={i.url} target="_blank" rel="noreferrer noopener" className="v8-mono">
                  {i.value}
                </a>
              ) : i.ref ? (
                <Link to={v8Links.entity(i.ref.kind, i.ref.id)} className="v8-mono">
                  {i.value}
                </Link>
              ) : (
                <span className="v8-mono">{i.value}</span>
              )}
            </span>
          ))}
          {(ident?.ids.length ?? 0) > 6 && <a href="#fiche-sources" className="fiche-id fiche-id-more">+{(ident?.ids.length ?? 0) - 6} identifiants</a>}
        </div>
        <div className="fiche-titlerow">
          <h1 className="v8-page-title fiche-title">{ident?.label ?? id}</h1>
          <div className="fiche-actions">
            <button type="button" className="v8-btn v8-btn-sm" onClick={() => void copyCitation()} title="Copier la citation (c)">
              Citer <KeyHint keys="c" />
            </button>
            <button type="button" className="v8-btn v8-btn-sm" onClick={() => void pin()} title="Épingler sur un tableau (p)">
              Épingler <KeyHint keys="p" />
            </button>
            {qid && (
              <Link className="v8-btn v8-btn-sm" to={v8Links.path(qid, '')} title="Comment cet enregistrement se relie à un autre">
                Chemin vers…
              </Link>
            )}
            {ident?.source && <SourceChip {...chip(ident.source)!} />}
          </div>
        </div>
        {(ident?.label_en && ident.label_en !== ident.label) || ident?.aliases.length ? (
          <p className="fiche-aliases v8-subtle">
            {ident?.label_en && ident.label_en !== ident.label ? <span lang="en">{ident.label_en}</span> : null}
            {ident?.aliases.length ? <span> · aussi : {ident.aliases.slice(0, 6).join(', ')}</span> : null}
          </p>
        ) : null}
        {(ident?.definition || ident?.description) && (
          <p className="fiche-desc">
            {ident?.definition ?? ident?.description}
            {ident?.definition && ident.definition_source && <SourceChip {...chip(ident.definition_source)!} />}
          </p>
        )}
        {ident?.anchor_note && !ident.description && <p className="fiche-note">{ident.anchor_note}</p>}
        {b.notes.map((n) => (
          <p key={n} className="fiche-note">
            {n}
          </p>
        ))}
      </header>

      <Headline stats={b.headline} />

      <nav className="fiche-tabs" aria-label="Sections de la fiche">
        {secs.map((s) => (
          <a
            key={s.id}
            href={`#fiche-${s.id}`}
            className={`fiche-tab${active === s.id ? ' is-active' : ''}${s.count === 0 ? ' is-empty' : ''}`}
            aria-current={active === s.id ? 'true' : undefined}
            onClick={(e) => {
              e.preventDefault();
              goSection(s.id);
            }}
          >
            {s.label}
            {s.count !== null && <span className="fiche-tab-n v8-num">{s.count.toLocaleString('fr-FR')}</span>}
          </a>
        ))}
        <span className="fiche-tabs-keys v8-subtle" aria-hidden="true">
          <KeyHint keys="j" /> <KeyHint keys="k" />
        </span>
      </nav>

      <Section id="faits" title="Faits" sub="Valeurs littérales déclarées par les sources (quantités, dates, codes).">
        <Facts facts={b.facts} />
      </Section>
      <Section id="relations" title="Relations" sub="Par famille de relations ; chaque lien ouvre la fiche liée.">
        <Relations b={b} kind={kind} id={id} />
      </Section>
      <Section id="series" title="Séries" sub="Dernière valeur, tendance et source ; « Monde » ouvre la carte de l’indicateur.">
        <SeriesSection series={b.series} tops={b.tops} />
      </Section>
      <Section id="carte" title="Carte & temps" sub="Où (déclarations « est situé dans », coordonnées) et quand (déclarations datées).">
        <SpaceTime b={b} />
      </Section>
      <Section id="causes" title="Causes & effets" sub="Affirmations attribuées à leurs sources, avec niveau et note ; REVA n’affirme aucune causalité.">
        <Causes b={b} />
      </Section>
      <Section id="previsions" title="Prévisions" sub="Questions scellées du registre prospectif et leur statut de jugement.">
        <Forecasts b={b} />
      </Section>
      <Section id="manques" title="Ce qui manque" sub="Ce que REVA attend pour ce type d’entité et ne trouve pas, avec la raison quand elle est connue.">
        <Missing items={b.missing} />
      </Section>
      <Section id="sources" title="Sources" sub="Chaque source derrière une valeur affichée, avec sa licence.">
        <Sources b={b} />
        {(ident?.ids.length ?? 0) > 0 && (
          <div className="fiche-allids">
            <h3 className="fiche-h3">Identifiants</h3>
            <ul>
              {ident!.ids.map((i) => (
                <li key={`${i.scheme}|${i.value}|${i.property ?? ''}`}>
                  <span className="fiche-id-s">{i.scheme}</span>{' '}
                  {i.url ? (
                    <a href={i.url} target="_blank" rel="noreferrer noopener" className="v8-mono">
                      {i.value}
                    </a>
                  ) : i.ref ? (
                    <Link to={v8Links.entity(i.ref.kind, i.ref.id)} className="v8-mono">
                      {shortId(i.ref.id)}
                    </Link>
                  ) : (
                    <span className="v8-mono">{i.value}</span>
                  )}
                  {i.label_fr && <span className="v8-subtle"> — {i.label_fr}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
        <p className="v8-subtle fiche-small fiche-meta">
          Lecture {b.meta.cached ? 'en cache' : 'directe'} · {b.meta.generated_at?.slice(0, 16).replace('T', ' ')} UTC
          {b.meta.atlas_snapshot && ` · instantané atlas ${b.meta.atlas_snapshot.slice(0, 12)} (${b.meta.atlas_cutoff?.slice(0, 16).replace('T', ' ')} UTC)`}
          {b.meta.errors.length > 0 && ` · sections en erreur : ${b.meta.errors.map((e) => e.section).join(', ')}`}
        </p>
      </Section>
      {toast && (
        <div className="fiche-toast" role="status">
          {toast}
        </div>
      )}
    </div>
  );
}

function Section({ id, title, sub, children }: { id: string; title: string; sub: string; children: React.ReactNode }) {
  return (
    <section className="fiche-section" id={`fiche-${id}`} aria-labelledby={`fiche-${id}-h`}>
      <header className="fiche-section-head">
        <h2 id={`fiche-${id}-h`} tabIndex={-1}>
          {title}
        </h2>
        <p className="v8-subtle">{sub}</p>
      </header>
      {children}
    </section>
  );
}
