/**
 * Accueil — « le pouls du monde ». One screen answering "what is new, and what needs me?":
 * coverage at a glance, today's changes (window and kinds in the URL), owner decisions waiting, the next
 * resolutions, pinned boards. Each panel loads and fails on its own; empty states say what is missing and why.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { v8Links } from '../../contract';
import { Badge, EmptyState, ErrorState, KeyHint, Panel, Skeleton, SourceChip, useUrlParam } from '../../shell/ui';
import { classifyV3Error } from '../../../v3/api/common';
import { useApi } from '../../../lib/useApi';
import { listBoardsForPin, onPinned } from '../tableaux/pin';
import { getCalendar, getChanges, getCoverage, getDecisions, type ChangeItem, type DecisionItem } from './api';
import { asWindow, countsFr, DEFAULT_FEED_KINDS, dateFr, daysUntil, groupByDay, KIND_ORDER, kindFr, relDays, sinceFor, timeFr, verdictOf, WINDOW_FR, WINDOWS, type WindowKey } from './model';
import './accueil.css';

/** 93 495 663 → "93,5 M" (one decimal below 100 units, so the glance strip keeps the order of magnitude AND the digit). */
export function compactFr(n: number): string {
  const a = Math.abs(n);
  const f = (v: number, u: string) => `${v.toLocaleString('fr-FR', { maximumFractionDigits: v < 100 ? 1 : 0 })} ${u}`;
  if (a >= 1e9) return f(n / 1e9, 'Md');
  if (a >= 1e6) return f(n / 1e6, 'M');
  if (a >= 1e4) return f(n / 1e3, 'k');
  return n.toLocaleString('fr-FR');
}

function isTyping(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
}

function absent(err: unknown): boolean {
  const k = classifyV3Error(err).kind;
  return k === 'unavailable' || (k === 'not_found' && /no such endpoint|not found/i.test(String((err as { problem?: { detail?: string } })?.problem?.detail ?? '')));
}

/** Opens the shell palette (Ctrl/⌘-K is the shell's global shortcut). */
function openPalette() {
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
}

// ------------------------------------------------------------------ quick actions

function QuickActions() {
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const ask = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'q' && !e.ctrlKey && !e.metaKey && !e.altKey && !isTyping(e.target) && !e.defaultPrevented) {
        e.preventDefault();
        ask.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  return (
    <div className="v8a-quick" role="group" aria-label="Actions rapides">
      <form
        className="v8a-ask"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          if (q.trim()) navigate(v8Links.ask(q.trim()));
        }}
      >
        <label htmlFor="v8a-ask-in" className="v8-sr-only">
          Poser une question
        </label>
        <input
          id="v8a-ask-in"
          ref={ask}
          className="v8-input v8a-ask-in"
          placeholder="Demander… ex. inflation en France depuis 2000"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === 'Escape' && (e.currentTarget.blur(), setQ(''))}
        />
        <KeyHint keys="q" />
      </form>
      <button type="button" className="v8-btn" onClick={openPalette}>
        Rechercher <KeyHint keys="Mod K" />
      </button>
      <Link className="v8-btn" to="/v8/comparer">
        Comparer
      </Link>
      <Link className="v8-btn" to={v8Links.decision()}>
        Décision
      </Link>
      <Link className="v8-btn" to="/v8/tableaux">
        Tableaux
      </Link>
    </div>
  );
}

// ------------------------------------------------------------------ coverage at a glance

function Glance() {
  const cov = useApi(() => getCoverage(), []);
  const cal = useApi(() => getCalendar(), []);
  const dec = useApi(() => getDecisions(), []);
  const m = cov.status === 'ok' ? cov.data.meta : null;
  const totals = cal.status === 'ok' ? cal.data.totals : null;
  const openDecisions = dec.status === 'ok' ? (dec.data.counts?.open ?? dec.data.groups.reduce((n, g) => n + g.items.filter((i) => i.status !== 'done' && i.status !== 'dropped').length, 0)) : null;
  const covChip = m ? (
    <SourceChip
      source="REVA · agrégats de couverture"
      label="source"
      vintage={dateFr(m.computed_at, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}
      details={[`calculés le ${m.computed_at}`, ...(m.stale ? ['des tables ont été chargées depuis le calcul : chiffres en retard'] : [])]}
    />
  ) : null;
  const tile = (label: string, value: string | null, sub: string, state: 'loading' | 'ok' | 'error' | 'idle', href?: string, why?: string, chip?: ReactNode) => (
    <div className="v8a-tile" data-state={state}>
      <div className="v8a-tile-label">
        {label}
        {chip}
      </div>
      <div className="v8a-tile-value v8-num">
        {state === 'loading' ? <span className="v8a-tile-skel" aria-label="chargement" /> : value !== null ? value : <span className="v8a-tile-na" title={why}>indisponible</span>}
      </div>
      <div className="v8a-tile-sub">{href ? <Link to={href}>{sub}</Link> : sub}</div>
    </div>
  );
  const covWhy = cov.status === 'error' ? `${classifyV3Error(cov.error).title} : agrégats de couverture (/v1/v8/coverage)` : undefined;
  return (
    <section className="v8a-glance" aria-label="Couverture en bref">
      {tile('Atlas', m ? compactFr(m.atlas_rows) : null, m ? `${m.atlas_tables.toLocaleString('fr-FR')} tables · ${m.places} lieux` : 'tables · lignes', cov.status, '/v8/couverture', covWhy, covChip)}
      {tile('Graphe', m ? (m.kg_computed ? compactFr(m.kg_statements) : null) : null, m ? `${compactFr(m.kg_entities)} entités · déclarations` : 'entités · déclarations', cov.status, '/v8/couverture', covWhy ?? (m && !m.kg_computed ? 'non calculé' : undefined))}
      {tile(
        'Prévisions scellées',
        totals ? totals.questions.toLocaleString('fr-FR') : null,
        totals && totals.pending !== null && totals.resolved !== null
          ? `${totals.pending.toLocaleString('fr-FR')} en attente · ${totals.resolved.toLocaleString('fr-FR')} résolues`
          : totals
            ? 'questions · statuts en cours de calcul'
            : 'questions',
        cal.status === 'ok' && !totals ? 'error' : cal.status,
        v8Links.scoreboard(),
        cal.status === 'ok' && !totals ? 'Totaux du registre servis par /v1/v8/scores (absent)' : undefined,
      )}
      {tile(
        'Prochaine résolution',
        cal.status === 'ok' && cal.data.nextResolution ? dateFr(cal.data.nextResolution, { day: 'numeric', month: 'short' }) : null,
        cal.status === 'ok' && cal.data.nextResolution ? relDays(daysUntil(cal.data.nextResolution)) : cal.status === 'ok' && cal.data.refreshing ? 'en cours de calcul' : 'calendrier',
        cal.status === 'ok' && cal.data.refreshing ? 'loading' : cal.status,
        v8Links.scoreboard(),
      )}
      {tile('Décisions en attente', openDecisions !== null ? String(openDecisions) : null, 'du propriétaire', dec.status, '/v8/couverture?vue=decisions', dec.status === 'error' ? classifyV3Error(dec.error).title : undefined)}
    </section>
  );
}

// ------------------------------------------------------------------ changes

function ChangeRow({ it, highlighted }: { it: ChangeItem; highlighted: boolean }) {
  const k = kindFr(it.kind);
  const v = verdictOf(it);
  const first = it.links[0];
  const navigate = useNavigate();
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (highlighted) ref.current?.scrollIntoView?.({ block: 'center' });
  }, [highlighted]);
  return (
    <li
      ref={ref}
      className={`v8a-change v8a-sev-${it.severity}${highlighted ? ' is-highlighted' : ''}`}
      tabIndex={0}
      data-change-id={it.id}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && first && e.target === e.currentTarget) {
          e.preventDefault();
          navigate(first.href);
        }
      }}
    >
      <span className="v8a-change-time v8-num">{timeFr(it.at)}</span>
      <span className={`v8a-kind v8a-kind-${it.kind}`} title={k.label}>
        <span aria-hidden="true">{k.glyph}</span> {k.short}
      </span>
      <div className="v8a-change-main">
        <div className="v8a-change-title">
          {it.title}
          {it.origin === 'test_fixture' && <Badge kind="fixture" size="xs" />}
        </div>
        <div className="v8a-change-sub">
          {Object.keys(it.counts).length > 0 && <span className="v8-num">{countsFr(it.counts)}</span>}
          {it.summary && !(v && it.detail.status === 'void') && <span>{it.summary}</span>}
          {v && (
            <Badge kind={v.kind} size="xs">
              {v.text}
            </Badge>
          )}
          {it.source && <span className="v8a-src">{it.source}</span>}
        </div>
        {it.links.length > 0 && (
          <div className="v8a-change-links">
            {it.links.slice(0, 2).map((l) => (
              <Link key={l.href} to={l.href} tabIndex={-1} title={l.label}>
                {l.label}
              </Link>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

function Changes() {
  const [win, setWin] = useUrlParam('p', '24h');
  const [kind, setKind] = useUrlParam('k', '');
  const [params] = useSearchParams();
  const highlight = params.get('change');
  const w: WindowKey = asWindow(win);
  const since = useMemo(() => sinceFor(w), [w]);
  const [extra, setExtra] = useState<{ items: ChangeItem[]; cursor: string | null } | null>(null);
  const kindsParam = kind || DEFAULT_FEED_KINDS.join(',');
  const st = useApi((s) => getChanges({ since, limit: 100, kinds: kindsParam }, s), [since, kindsParam]);
  const [shown, setShown] = useState(20);
  useEffect(() => {
    setExtra(null);
    setShown(20);
  }, [since, kind]);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
      if (e.key !== 'j' && e.key !== 'k') return;
      const rows = Array.from(listRef.current?.querySelectorAll<HTMLElement>('.v8a-change') ?? []);
      if (!rows.length) return;
      const cur = rows.indexOf(document.activeElement as HTMLElement);
      e.preventDefault();
      rows[cur < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, cur + (e.key === 'j' ? 1 : -1)))]!.focus();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const data = st.status === 'ok' ? st.data : null;
  const items = data ? [...data.items, ...(extra?.items ?? [])] : [];
  const cursor = extra ? extra.cursor : (data?.next_cursor ?? null);
  // counts per kind come from the unfiltered request (a filtered request only counts its own kind)
  const fullCounts = useRef<{ since: string; counts: Record<string, number> } | null>(null);
  if (data && !kind) fullCounts.current = { since, counts: data.counts };
  const counts = (fullCounts.current?.since === since ? fullCounts.current.counts : null) ?? data?.counts ?? {};
  const kinds = KIND_ORDER.filter((k) => (counts[k] ?? 0) > 0 || k === kind).concat(Object.keys(counts).filter((k) => !KIND_ORDER.includes(k) && (counts[k] ?? 0) > 0));
  const total = Object.entries(counts)
    .filter(([k]) => k !== 'owner_decision')
    .reduce((a, [, b]) => a + b, 0);

  const loadMore = async () => {
    if (!cursor) return;
    const r = await getChanges({ since, limit: 100, cursor, kinds: kindsParam });
    setExtra((x) => ({ items: [...(x?.items ?? []), ...r.items], cursor: r.next_cursor }));
  };

  return (
    <Panel
      id="v8a-changes"
      className="v8a-changes"
      title="Ce qui a changé"
      subtitle={data ? `${total.toLocaleString('fr-FR')} changement${total > 1 ? 's' : ''} enregistré${total > 1 ? 's' : ''} sur ${WINDOW_FR[w]}` : `sur ${WINDOW_FR[w]}`}
      actions={
        <div className="v8a-seg" role="radiogroup" aria-label="Fenêtre">
          {(Object.keys(WINDOWS) as WindowKey[]).map((k) => (
            <button key={k} type="button" role="radio" aria-checked={w === k} className={`v8a-seg-btn${w === k ? ' is-on' : ''}`} onClick={() => setWin(k, { replace: false })}>
              {WINDOW_FR[k]}
            </button>
          ))}
        </div>
      }
    >
      {data && kinds.length > 0 && (
        <div className="v8a-chips" role="group" aria-label="Filtrer par type">
          <button type="button" className={`v8a-chip${kind === '' ? ' is-on' : ''}`} aria-pressed={kind === ''} onClick={() => setKind('', { replace: false })}>
            Tout <span className="v8-num">{total.toLocaleString('fr-FR')}</span>
            {(counts.owner_decision ?? 0) > 0 && <span className="v8-sr-only"> (hors décisions du propriétaire, listées à part)</span>}
          </button>
          {kinds.map((k) => (
            <button key={k} type="button" className={`v8a-chip v8a-chip-${k}${kind === k ? ' is-on' : ''}`} aria-pressed={kind === k} onClick={() => setKind(kind === k ? '' : k, { replace: false })}>
              {kindFr(k).short} <span className="v8-num">{(counts[k] ?? 0).toLocaleString('fr-FR')}</span>
            </button>
          ))}
        </div>
      )}
      {(st.status === 'loading' || st.status === 'idle') && <Skeleton lines={8} />}
      {st.status === 'error' &&
        (absent(st.error) ? (
          <EmptyState title="Fil des changements pas encore disponible" icon="∅">
            Ce serveur ne sert pas encore <code>/v1/v8/changes</code> (agent V8API). Rien n’est simulé à la place : les chargements, révisions, prévisions scellées et
            résolutions apparaîtront ici dès que la route sera déployée.
          </EmptyState>
        ) : (
          <ErrorState error={st.error} what="le fil des changements" onRetry={st.reload} />
        ))}
      {data && items.length === 0 && (
        <EmptyState
          title={`Aucun changement enregistré sur ${WINDOW_FR[w]}`}
          action={
            w !== '30j' ? (
              <button type="button" className="v8-btn v8-btn-sm" onClick={() => setWin(w === '24h' ? '7j' : '30j', { replace: false })}>
                Élargir à {w === '24h' ? '7 jours' : '30 jours'}
              </button>
            ) : undefined
          }
        >
          Aucun chargement, révision, lot de prévisions ni résolution n’a été enregistré dans cette fenêtre{kind ? ` pour « ${kindFr(kind).label} »` : ''}. Ce n’est pas une
          panne : c’est ce que la base enregistre.
        </EmptyState>
      )}
      {items.length > 0 && (
        <div ref={listRef} className="v8a-feed">
          {groupByDay(items.slice(0, Math.max(shown, highlight ? items.findIndex((i) => i.id === highlight) + 1 : 0))).map((g) => (
            <section key={g.day} aria-label={g.day}>
              <h3 className="v8a-day">{g.day}</h3>
              <ul className="v8a-change-list">
                {g.items.map((it) => (
                  <ChangeRow key={it.id} it={it} highlighted={highlight === it.id} />
                ))}
              </ul>
            </section>
          ))}
          {shown < items.length ? (
            <button type="button" className="v8-btn v8-btn-sm v8a-more" onClick={() => setShown((n) => n + 30)}>
              Afficher {Math.min(30, items.length - shown)} de plus ({items.length - shown} restants chargés)
            </button>
          ) : (
            cursor && (
              <button type="button" className="v8-btn v8-btn-sm v8a-more" onClick={() => void loadMore().then(() => setShown((n) => n + 30))}>
                Plus ancien…
              </button>
            )
          )}
        </div>
      )}
    </Panel>
  );
}

// ------------------------------------------------------------------ owner decisions

function Decisions() {
  const st = useApi(() => getDecisions(), []);
  const open = (st.status === 'ok' ? st.data.groups.flatMap((g) => g.items.map((i) => ({ ...i, kind_label: i.kind_label ?? g.label }))) : []).filter(
    (i: DecisionItem) => i.status !== 'done' && i.status !== 'dropped' && i.observed?.state !== 'done',
  );
  return (
    <Panel
      id="v8a-decisions"
      title="Décisions qui vous attendent"
      subtitle={st.status === 'ok' ? `${open.length} ouverte${open.length > 1 ? 's' : ''} · licences, clés, autorisations` : 'licences, clés, autorisations'}
      actions={
        <Link className="v8-btn v8-btn-ghost v8-btn-sm" to="/v8/couverture?vue=decisions">
          Tout voir
        </Link>
      }
      dense
    >
      {(st.status === 'loading' || st.status === 'idle') && <Skeleton lines={4} />}
      {st.status === 'error' &&
        (absent(st.error) ? (
          <EmptyState compact title="Liste des décisions pas encore disponible">
            <code>/v1/v8/coverage/decisions</code> n’est pas servi par ce serveur.
          </EmptyState>
        ) : (
          <ErrorState error={st.error} what="les décisions du propriétaire" onRetry={st.reload} />
        ))}
      {st.status === 'ok' && open.length === 0 && (
        <EmptyState compact title="Aucune décision ouverte">
          Le registre des décisions (mis à jour le {dateFr(st.data.updated ?? '')}) ne contient rien qui attende le propriétaire.
        </EmptyState>
      )}
      {open.length > 0 && (
        <ul className="v8a-dec-list">
          {open.slice(0, 4).map((d) => (
            <li key={d.id} className="v8a-dec" title={d.action ? `À faire : ${d.action}` : undefined}>
              <span className="v8a-dec-kind">{d.kind_label}</span>
              <span className="v8a-dec-title">{d.title}</span>
              {d.unlocks?.text && <span className="v8a-dec-why">Débloque : {d.unlocks.text}</span>}
            </li>
          ))}
          {open.length > 4 && (
            <li className="v8a-dec-more">
              <Link to="/v8/couverture?vue=decisions">+ {open.length - 4} autres</Link>
            </li>
          )}
        </ul>
      )}
    </Panel>
  );
}

// ------------------------------------------------------------------ calendar

function Calendar() {
  const st = useApi(() => getCalendar(), []);
  const refreshing = st.status === 'ok' && st.data.refreshing;
  const reload = st.reload;
  useEffect(() => {
    if (!refreshing) return;
    const t = setTimeout(() => {
      getCalendar.reset();
      reload();
    }, 8000);
    return () => clearTimeout(t);
  }, [refreshing, reload]);
  const d = st.status === 'ok' ? st.data : null;
  const upcoming = d ? d.upcoming.filter((u) => !u.overdue).slice(0, 7) : [];
  const overdue = d ? d.upcoming.filter((u) => u.overdue) : [];
  const events = d ? d.events.filter((e) => e.date || e.month).slice(0, 4) : [];
  return (
    <Panel
      id="v8a-calendar"
      title="Prochaines résolutions"
      subtitle={
        d?.totals && d.totals.pending !== null && d.totals.resolved !== null
          ? `${d.totals.pending.toLocaleString('fr-FR')} questions scellées en attente · ${d.totals.resolved.toLocaleString('fr-FR')} résolues`
          : 'prévisions scellées et tests pré-enregistrés'
      }
      actions={
        <Link className="v8-btn v8-btn-ghost v8-btn-sm" to={v8Links.scoreboard()}>
          Scores
        </Link>
      }
      dense
    >
      {(st.status === 'loading' || st.status === 'idle') && <Skeleton lines={5} />}
      {st.status === 'error' && <ErrorState error={st.error} what="le calendrier des résolutions" onRetry={st.reload} />}
      {d && d.sample && (
        <p className="v8a-note">
          Service des scores absent : aperçu sur les {d.sample.n} premières questions en attente (sur {d.sample.total.toLocaleString('fr-FR')}).
        </p>
      )}
      {d && d.refreshing && (
        <p className="v8a-note" role="status">
          Le service des scores calcule l’état du registre (premier appel après démarrage) : dates de résolution bientôt disponibles.{' '}
          <button
            type="button"
            className="v8-btn v8-btn-ghost v8-btn-sm"
            onClick={() => {
              getCalendar.reset();
              st.reload();
            }}
          >
            Actualiser
          </button>
        </p>
      )}
      {d && !d.refreshing && upcoming.length === 0 && events.length === 0 && (
        <EmptyState compact title="Aucune résolution à venir">
          Le registre ne contient pas de question scellée en attente.
        </EmptyState>
      )}
      {upcoming.length > 0 && (
        <ol className="v8a-cal">
          {upcoming.map((u) => {
            const n = daysUntil(u.date);
            return (
              <li key={`${u.date}|${u.family}`} className="v8a-cal-row">
                <span className="v8a-cal-date v8-num">
                  <strong>{dateFr(u.date, { day: 'numeric', month: 'short' })}</strong>
                  <span>{relDays(n)}</span>
                </span>
                <span className="v8a-cal-main">
                  <Link to={v8Links.scoreboard(u.family)}>{u.label_fr ?? u.family}</Link>
                  <span className="v8a-cal-sub v8-num">
                    {u.n_questions.toLocaleString('fr-FR')} question{u.n_questions > 1 ? 's' : ''}
                    {u.officials.length > 0 ? ` · contre ${u.officials.join(', ')}` : ' · pas de prévisionniste officiel scellé'}
                  </span>
                </span>
              </li>
            );
          })}
        </ol>
      )}
      {overdue.length > 0 && (
        <p className="v8a-note v8a-overdue">
          {overdue.length} groupe{overdue.length > 1 ? 's' : ''} en retard de résolution (date dépassée, résultat officiel pas encore chargé) :{' '}
          {overdue
            .slice(0, 3)
            .map((u) => u.label_fr ?? u.family)
            .join(', ')}
          .
        </p>
      )}
      {events.length > 0 && (
        <>
          <h3 className="v8a-sub-h">Tests pré-enregistrés et échéances</h3>
          <ul className="v8a-events">
            {events.map((e) => (
              <li key={e.id}>
                <span className="v8a-cal-date v8-num">
                  <strong>{dateFr(e.date ?? e.month ?? '', e.date ? { day: 'numeric', month: 'short' } : undefined)}</strong>
                </span>
                <span>{e.note_fr ?? e.id}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

// ------------------------------------------------------------------ boards

function Boards() {
  const [tick, setTick] = useState(0);
  useEffect(() => onPinned(() => setTick((t) => t + 1)), []);
  const st = useApi((s) => listBoardsForPin(s), [tick]);
  const boards = st.status === 'ok' ? st.data.boards.slice(0, 5) : [];
  return (
    <Panel
      id="v8a-boards"
      title="Tableaux épinglés"
      subtitle={st.status === 'ok' ? (st.data.mode === 'server' ? 'enregistrés sur le serveur' : 'gardés dans ce navigateur') : undefined}
      actions={
        <Link className="v8-btn v8-btn-ghost v8-btn-sm" to="/v8/tableaux">
          Tous
        </Link>
      }
      dense
    >
      {(st.status === 'loading' || st.status === 'idle') && <Skeleton lines={3} />}
      {st.status === 'error' && <ErrorState error={st.error} what="les tableaux" onRetry={st.reload} />}
      {st.status === 'ok' && boards.length === 0 && (
        <EmptyState compact title="Aucun tableau">
          Épinglez une série, une entité, une prévision ou une décision (bouton « Épingler » des pages) : elle arrive ici, avec ses alertes.
        </EmptyState>
      )}
      {boards.length > 0 && (
        <ul className="v8a-boards">
          {boards.map((b) => (
            <li key={`${b.origin}:${b.id}`}>
              <Link to={v8Links.board(b.id)} className="v8a-board">
                <span className="v8a-board-title">{b.title}</span>
                <span className="v8a-board-meta v8-num">
                  {b.n_items} élément{b.n_items > 1 ? 's' : ''}
                  {b.n_rules ? ` · ${b.n_rules} règle${b.n_rules > 1 ? 's' : ''}` : ''}
                  {b.origin === 'local' ? ' · local' : ''}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
      {st.status === 'ok' && st.data.note && st.data.mode === 'local' && <p className="v8a-note">{st.data.note}</p>}
    </Panel>
  );
}

// ------------------------------------------------------------------ page

export default function AccueilPage() {
  const now = useMemo(() => new Date(), []);
  return (
    <div className="v8-page v8a-page">
      <header className="v8-page-head v8a-head">
        <div>
          <h1 className="v8-page-title">Le pouls du monde</h1>
          <p className="v8-page-lede">
            Ce qui a changé, ce qui vous attend — au {now.toLocaleDateString('fr-FR', { weekday: 'long', day: 'numeric', month: 'long' })},{' '}
            {now.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}.
          </p>
        </div>
        <QuickActions />
      </header>
      <Glance />
      <div className="v8a-grid">
        <div className="v8a-col-main">
          <Changes />
        </div>
        <div className="v8a-col-side">
          <Decisions />
          <Calendar />
          <Boards />
        </div>
      </div>
      <p className="v8a-keys v8-muted" aria-hidden="true">
        <KeyHint keys="j" />/<KeyHint keys="k" /> changement suivant/précédent · <KeyHint keys="Enter" /> ouvrir · <KeyHint keys="q" /> demander ·{' '}
        <KeyHint keys="Mod K" /> rechercher · <KeyHint keys="g t" /> tableaux
      </p>
    </div>
  );
}
