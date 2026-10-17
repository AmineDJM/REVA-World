/**
 * One board: grid of pinned item cards (drag-and-drop and keyboard reorder), add panel (search, pasted V8 link,
 * Markdown note), watch rules, alerts, share-as-URL. A shared board (URL fragment) renders read-only.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent as RKeyboardEvent } from 'react';
import { Link, useNavigate } from 'react-router';
import { v8Links } from '../../contract';
import { classifyV3Error } from '../../../v3/api/common';
import { useApi } from '../../../lib/useApi';
import { getBoardAlerts, getChangesFor, type Alert } from './api';
import { AddPanel } from './AddPanel';
import { DecisionBody, EntityBody, ForecastBody, NoteBody, SeriesBody, useInView } from './items';
import {
  addItem,
  addRule,
  describeRule,
  itemTitle,
  KIND_LABEL_FR,
  removeItem,
  removeRule,
  reorder,
  shareHref,
  updateItem,
  type BoardItem,
  type PinItem,
  type WatchRule,
} from './model';
import { RulesPanel } from './RulesPanel';
import { evaluateLocal } from './rules';
import { BoardConflict, deleteBoard, importBoard, listBoards, rememberBoard, saveBoard, uploadBoard, type Origin, type StoredBoard } from './store';

export type ViewOrigin = Origin | 'shared';

export function itemHref(it: BoardItem): string | null {
  switch (it.kind) {
    case 'entity':
      return v8Links.entity(it.entityKind, it.id);
    case 'series': {
      const p: Record<string, string> = { concept: it.concept, geo: it.geo };
      if (it.headline) p.headline = '1';
      return it.table ? v8Links.series(it.table, p) : `/v8/monde?${new URLSearchParams(p).toString()}`;
    }
    case 'forecast':
      return v8Links.entity('question', it.questionId);
    case 'decision':
      return v8Links.decision(it.ref);
    default:
      return null;
  }
}

const ORIGIN_FR: Record<ViewOrigin, { label: string; title: string }> = {
  server: { label: 'serveur', title: 'Enregistré sur le serveur REVA (vos tableaux, liés à votre session).' },
  local: { label: 'local — ce navigateur', title: 'Gardé uniquement dans ce navigateur : invisible ailleurs, perdu si les données du site sont effacées.' },
  shared: { label: 'partagé — lecture seule', title: 'Rendu du tableau contenu dans le lien ; rien n’est enregistré. Les valeurs sont relues en direct.' },
};

function isTyping(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
}

// ------------------------------------------------------------------ card

interface CardProps {
  item: BoardItem;
  index: number;
  count: number;
  editable: boolean;
  grabbed: boolean;
  dragOver: boolean;
  rules: WatchRule[];
  alerts: Alert[];
  onHandleKey: (e: RKeyboardEvent<HTMLButtonElement>, index: number) => void;
  onRemove: (uid: string) => void;
  onWatch: (uid: string) => void;
  onNote: (uid: string, md: string) => void;
  onDragStart: (index: number) => void;
  onDragOverCard: (index: number) => void;
  onDrop: () => void;
  onDragEnd: () => void;
}

function ItemCard(p: CardProps) {
  const { item } = p;
  const [ref, visible] = useInView<HTMLElement>();
  const href = itemHref(item);
  const title = itemTitle(item);
  const fired = p.alerts.filter((a) => a.status === 'fired');
  const headId = `v8b-h-${item.uid}`;
  return (
    <article
      ref={ref}
      className={`v8b-card v8b-card-${item.kind}${p.grabbed ? ' is-grabbed' : ''}${p.dragOver ? ' is-over' : ''}`}
      aria-labelledby={headId}
      data-uid={item.uid}
      data-testid="board-item"
      tabIndex={-1}
      onDragOver={(e: DragEvent) => {
        if (!p.editable) return;
        e.preventDefault();
        p.onDragOverCard(p.index);
      }}
      onDrop={(e: DragEvent) => {
        e.preventDefault();
        p.onDrop();
      }}
    >
      <header className="v8b-card-head">
        {p.editable && (
          <button
            type="button"
            className="v8b-handle"
            draggable
            aria-label={`Déplacer « ${title} », position ${p.index + 1} sur ${p.count}. Espace pour saisir, flèches pour déplacer.`}
            aria-pressed={p.grabbed}
            title="Glisser pour déplacer (ou Espace puis flèches)"
            onKeyDown={(e) => p.onHandleKey(e, p.index)}
            onDragStart={(e) => {
              e.dataTransfer.effectAllowed = 'move';
              e.dataTransfer.setData('text/plain', item.uid);
              p.onDragStart(p.index);
            }}
            onDragEnd={p.onDragEnd}
          >
            <span aria-hidden="true">⠿</span>
          </button>
        )}
        <span className={`v8b-kind v8b-kind-${item.kind}`}>{KIND_LABEL_FR[item.kind]}</span>
        <h3 id={headId} className="v8b-card-title">
          {href ? <Link to={href}>{title}</Link> : title}
        </h3>
        {fired.length > 0 && (
          <span className="v8b-alert-dot" title={`${fired.length} alerte(s)`} aria-label={`${fired.length} alerte(s)`}>
            {fired.length}
          </span>
        )}
        {p.editable && (
          <button type="button" className="v8b-icon" aria-label={`Retirer « ${title} » du tableau`} title="Retirer (annulable)" onClick={() => p.onRemove(item.uid)}>
            ×
          </button>
        )}
      </header>
      <div className="v8b-card-body">
        {item.kind === 'series' && <SeriesBody item={item} visible={visible} />}
        {item.kind === 'entity' && <EntityBody item={item} visible={visible} />}
        {item.kind === 'forecast' && <ForecastBody item={item} visible={visible} compact />}
        {item.kind === 'decision' && <DecisionBody item={item} visible={visible} />}
        {item.kind === 'note' && <NoteBody item={item} editable={p.editable} onSave={(md) => p.onNote(item.uid, md)} />}
      </div>
      {(p.rules.length > 0 || (p.editable && item.kind !== 'note' && item.kind !== 'decision')) && (
        <footer className="v8b-card-foot">
          {p.rules.length > 0 ? (
            <span className="v8b-watching" title={p.rules.map((r) => describeRule(r, item)).join('\n')}>
              ◉ surveillé · {p.rules.length} règle{p.rules.length > 1 ? 's' : ''}
            </span>
          ) : (
            <span className="muted">non surveillé</span>
          )}
          {p.editable && item.kind !== 'note' && item.kind !== 'decision' && (
            <button type="button" className="v8-btn v8-btn-ghost v8-btn-sm" onClick={() => p.onWatch(item.uid)}>
              Surveiller…
            </button>
          )}
        </footer>
      )}
    </article>
  );
}

// ------------------------------------------------------------------ alerts

function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('fr-FR', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export function AlertList({ alerts, board, note }: { alerts: Alert[]; board: StoredBoard; note?: string | null }) {
  const byUid = new Map(board.items.map((i) => [i.uid, i] as const));
  if (alerts.length === 0)
    return (
      <p className="v8b-soft" role="status">
        Aucune alerte : aucune règle ne s’est déclenchée{note ? ` (${note})` : ''}.
      </p>
    );
  return (
    <ul className="v8b-alerts" data-testid="alerts">
      {alerts.map((a, i) => {
        const it = byUid.get(a.item_uid);
        const link = a.change?.links[0];
        return (
          <li key={`${a.rule_id}-${a.change?.id ?? i}`} className={`v8b-alert v8b-alert-${a.status}`}>
            <span className="v8b-alert-kind">{a.status === 'fired' ? '●' : '○'}</span>
            <span className="v8b-alert-msg">
              <strong>{it ? itemTitle(it) : 'élément retiré'}</strong> — {a.message}
              {a.observed?.value && (
                <span className="v8-num" title={`source ${a.observed.source ?? '?'}`}>
                  {' '}
                  (observé {a.observed.value.replace('.', ',')} {a.observed.unit ?? ''} en {a.observed.period ?? '?'}, {a.observed.source ?? 'source ?'})
                </span>
              )}
              {a.status === 'not_evaluable' && a.reason && a.reason !== a.message && <span className="v8b-missing"> {a.reason}</span>}
            </span>
            <span className="v8b-alert-at">{fmtWhen(a.at)}</span>
            {link ? (
              <Link className="v8b-alert-link" to={link.href}>
                {link.label} →
              </Link>
            ) : a.change ? (
              <Link className="v8b-alert-link" to={`/v8?change=${encodeURIComponent(a.change.id)}`}>
                voir le changement →
              </Link>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function useBoardAlerts(board: StoredBoard | null, origin: ViewOrigin, tick: number) {
  const enabled = !!board && board.rules.length > 0 && origin !== 'shared';
  const since = useMemo(() => new Date(Date.now() - 30 * 86400_000).toISOString(), []);
  return useApi(
    async (s) => {
      if (!board) return { alerts: [] as Alert[], note: null as string | null };
      if (origin === 'server') return { alerts: (await getBoardAlerts(board.id, undefined, s)).alerts, note: null };
      const changes = await getChangesFor({ since, kinds: 'ingest,kg_batch,question_resolved', limit: 200 }, s);
      return { alerts: evaluateLocal(board, changes), note: 'évaluées dans ce navigateur sur le fil des changements des 30 derniers jours' };
    },
    [board?.id, board?.rules.length, origin, tick],
    enabled,
  );
}

// ------------------------------------------------------------------ board view

export function BoardView({ initial, origin: initialOrigin }: { initial: StoredBoard; origin: ViewOrigin }) {
  const navigate = useNavigate();
  const [board, setBoard] = useState<StoredBoard>(initial);
  const [origin, setOrigin] = useState<ViewOrigin>(initialOrigin);
  const editable = origin !== 'shared';
  const [save, setSave] = useState<{ state: 'idle' | 'saving' | 'saved' | 'error'; msg?: string }>({ state: 'idle' });
  const [conflict, setConflict] = useState<StoredBoard | null>(null);
  const [panel, setPanel] = useState<'none' | 'add' | 'rules' | 'share'>('none');
  const [watchUid, setWatchUid] = useState<string | null>(null);
  const [grab, setGrab] = useState<{ uid: string; from: number } | null>(null);
  const [drag, setDrag] = useState<{ from: number; over: number } | null>(null);
  const [announce, setAnnounce] = useState('');
  const [undo, setUndo] = useState<{ board: StoredBoard; label: string } | null>(null);
  const [alertTick, setAlertTick] = useState(0);
  const [serverMode, setServerMode] = useState<Origin | null>(null);
  const [editingTitle, setEditingTitle] = useState(false);
  const gridRef = useRef<HTMLDivElement>(null);

  // ---- save queue (serialised; the server version flows from one save to the next)
  const latest = useRef(board);
  const version = useRef<number | null>(board.version ?? null);
  const running = useRef(false);
  const dirty = useRef(false);
  const flush = useCallback(async () => {
    if (running.current || origin === 'shared') return;
    running.current = true;
    try {
      while (dirty.current) {
        dirty.current = false;
        setSave({ state: 'saving' });
        try {
          const saved = await saveBoard({ ...latest.current, version: version.current }, origin);
          version.current = (saved as StoredBoard).version ?? version.current;
          setSave({ state: 'saved' });
        } catch (err) {
          dirty.current = false;
          if (err instanceof BoardConflict) {
            setConflict(err.current);
            setSave({ state: 'error', msg: err.message });
          } else setSave({ state: 'error', msg: err instanceof Error ? err.message : String(err) });
        }
      }
    } finally {
      running.current = false;
    }
  }, [origin]);

  const commit = useCallback(
    (next: StoredBoard, say?: string) => {
      latest.current = next;
      setBoard(next);
      if (say) setAnnounce(say);
      if (origin === 'shared') return;
      dirty.current = true;
      void flush();
    },
    [flush, origin],
  );

  useEffect(() => {
    if (origin !== 'shared') rememberBoard(board.id);
  }, [board.id, origin]);

  useEffect(() => {
    if (origin !== 'local') return;
    const ctrl = new AbortController();
    listBoards(ctrl.signal)
      .then((l) => setServerMode(l.mode))
      .catch(() => {});
    return () => ctrl.abort();
  }, [origin]);

  const alerts = useBoardAlerts(board, origin, alertTick);
  const alertList = alerts.status === 'ok' ? alerts.data.alerts : [];

  // ---- actions
  const removeAt = (uid: string) => {
    const it = board.items.find((i) => i.uid === uid);
    if (!it) return;
    setUndo({ board, label: itemTitle(it) });
    commit(removeItem(board, uid), `« ${itemTitle(it)} » retiré. Ctrl+Z pour annuler.`);
  };
  const doUndo = () => {
    if (!undo) return;
    commit({ ...undo.board, version: latest.current.version }, `« ${undo.label} » remis sur le tableau.`);
    setUndo(null);
  };
  const addPin = (pin: PinItem) => {
    const r = addItem(board, pin);
    if (!r.added) {
      setAnnounce(`« ${itemTitle(pin)} » est déjà sur ce tableau.`);
      return false;
    }
    commit(r.board, `« ${itemTitle(pin)} » ajouté.`);
    return true;
  };
  const addNote = () => {
    commit(addItem(board, { kind: 'note', markdown: '' }).board, 'Note ajoutée.');
  };

  const move = (from: number, to: number, say = true) => {
    if (to < 0 || to >= board.items.length || from === to) return;
    const next = reorder(board, from, to);
    commit(next, say ? `« ${itemTitle(board.items[from]!)} » déplacé en position ${to + 1} sur ${board.items.length}.` : undefined);
  };

  const focusHandle = (uid: string) =>
    requestAnimationFrame(() => (gridRef.current?.querySelector(`[data-uid="${uid}"] .v8b-handle`) as HTMLElement | null)?.focus());

  const onHandleKey = (e: RKeyboardEvent<HTMLButtonElement>, index: number) => {
    const it = board.items[index]!;
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault();
      if (grab) {
        setGrab(null);
        setAnnounce(`« ${itemTitle(it)} » posé en position ${index + 1}.`);
      } else {
        setGrab({ uid: it.uid, from: index });
        setAnnounce(`« ${itemTitle(it)} » saisi, position ${index + 1} sur ${board.items.length}. Flèches pour déplacer, Espace pour poser, Échap pour annuler.`);
      }
      return;
    }
    if (e.key === 'Escape' && grab) {
      e.preventDefault();
      e.stopPropagation();
      move(index, grab.from, false);
      setAnnounce(`Déplacement annulé : « ${itemTitle(it)} » revenu en position ${grab.from + 1}.`);
      setGrab(null);
      focusHandle(it.uid);
      return;
    }
    const delta = e.key === 'ArrowUp' || e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowDown' || e.key === 'ArrowRight' ? 1 : 0;
    if (delta && (grab || e.altKey)) {
      e.preventDefault();
      move(index, index + delta);
      focusHandle(it.uid);
    } else if (e.key === 'Home' && (grab || e.altKey)) {
      e.preventDefault();
      move(index, 0);
      focusHandle(it.uid);
    } else if (e.key === 'End' && (grab || e.altKey)) {
      e.preventDefault();
      move(index, board.items.length - 1);
      focusHandle(it.uid);
    }
  };

  // ---- board-level keys: a = add, n = note, r = rules, s = share, j/k = next/previous card, ctrl+z = undo
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.defaultPrevented) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && undo) {
        e.preventDefault();
        doUndo();
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'j' || e.key === 'k') {
        const cards = Array.from(gridRef.current?.querySelectorAll<HTMLElement>('.v8b-card') ?? []);
        if (!cards.length) return;
        const cur = cards.findIndex((c) => c.contains(document.activeElement));
        const nextI = cur < 0 ? 0 : Math.max(0, Math.min(cards.length - 1, cur + (e.key === 'j' ? 1 : -1)));
        const target = cards[nextI]!.querySelector<HTMLElement>('.v8b-handle') ?? cards[nextI]!.querySelector<HTMLElement>('a') ?? cards[nextI]!;
        e.preventDefault();
        target.focus();
        return;
      }
      if (!editable) return;
      if (e.key === 'a' || e.key === '+') {
        e.preventDefault();
        setPanel('add');
      } else if (e.key === 'n') {
        e.preventDefault();
        addNote();
      } else if (e.key === 'r') {
        e.preventDefault();
        setPanel((p) => (p === 'rules' ? 'none' : 'rules'));
      } else if (e.key === 's') {
        e.preventDefault();
        setPanel((p) => (p === 'share' ? 'none' : 'share'));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const share = useMemo(() => (typeof window !== 'undefined' ? window.location.origin : '') + shareHref(board), [board]);
  const [copied, setCopied] = useState(false);

  const fired = alertList.filter((a) => a.status === 'fired').length;

  return (
    <section className="v8b-board" aria-labelledby="v8b-board-title">
      <header className="v8b-board-head">
        <div className="v8b-board-titles">
          <p className="v8b-crumb">
            <Link to="/v8/tableaux">Tableaux</Link> /
          </p>
          {editingTitle && editable ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const t = new FormData(e.currentTarget).get('t');
                if (typeof t === 'string' && t.trim()) commit({ ...board, title: t.trim().slice(0, 200), updated_at: new Date().toISOString() }, 'Titre modifié.');
                setEditingTitle(false);
              }}
            >
              <label className="sr-only" htmlFor="v8b-title-in">
                Titre du tableau
              </label>
              <input id="v8b-title-in" name="t" defaultValue={board.title} autoFocus maxLength={200} onBlur={(e) => e.currentTarget.form?.requestSubmit()} onKeyDown={(e) => e.key === 'Escape' && setEditingTitle(false)} />
            </form>
          ) : (
            <h1 id="v8b-board-title" className="v8b-h1">
              {board.title}
              {editable && (
                <button type="button" className="v8b-icon" aria-label="Renommer le tableau" title="Renommer" onClick={() => setEditingTitle(true)}>
                  ✎
                </button>
              )}
            </h1>
          )}
          <p className="v8b-board-sub">
            <span className={`v8-badge v8b-origin v8b-origin-${origin}`} title={ORIGIN_FR[origin].title}>
              {ORIGIN_FR[origin].label}
            </span>
            <span>
              {board.items.length} élément{board.items.length > 1 ? 's' : ''} · {board.rules.length} règle{board.rules.length > 1 ? 's' : ''} de veille
            </span>
            {board.rules.length > 0 && origin !== 'shared' && (
              <span className={fired ? 'v8b-fired' : 'muted'}>
                {alerts.status === 'loading' ? 'alertes…' : alerts.status === 'error' ? 'alertes indisponibles' : `${fired} alerte${fired > 1 ? 's' : ''}`}
              </span>
            )}
            <span className="v8b-save" aria-live="polite">
              {save.state === 'saving' ? 'enregistrement…' : save.state === 'saved' ? (origin === 'server' ? 'enregistré' : 'gardé dans ce navigateur') : ''}
            </span>
          </p>
        </div>
        <div className="v8b-actions" role="toolbar" aria-label="Actions du tableau">
          {editable && (
            <>
              <button type="button" className={`v8-btn${panel === 'add' ? ' is-on' : ''}`} onClick={() => setPanel(panel === 'add' ? 'none' : 'add')} aria-expanded={panel === 'add'}>
                Ajouter <kbd>a</kbd>
              </button>
              <button type="button" className="v8-btn" onClick={addNote}>
                Note <kbd>n</kbd>
              </button>
              <button type="button" className={`v8-btn${panel === 'rules' ? ' is-on' : ''}`} onClick={() => setPanel(panel === 'rules' ? 'none' : 'rules')} aria-expanded={panel === 'rules'}>
                Veille <kbd>r</kbd>
              </button>
            </>
          )}
          <button type="button" className={`v8-btn${panel === 'share' ? ' is-on' : ''}`} onClick={() => setPanel(panel === 'share' ? 'none' : 'share')} aria-expanded={panel === 'share'}>
            Partager <kbd>s</kbd>
          </button>
          {origin === 'shared' && (
            <button
              type="button"
              className="v8-btn v8-btn-primary"
              onClick={async () => {
                const r = await importBoard(board, 'server');
                navigate(v8Links.board(r.board.id));
              }}
            >
              Garder une copie
            </button>
          )}
          {origin === 'local' && serverMode === 'server' && (
            <button
              type="button"
              className="v8-btn"
              title="Copier ce tableau sur le serveur (puis le retirer de ce navigateur)"
              onClick={async () => {
                try {
                  const saved = await uploadBoard(latest.current);
                  latest.current = saved;
                  version.current = saved.version ?? null;
                  setBoard(saved);
                  setOrigin('server');
                  setAnnounce('Tableau enregistré sur le serveur.');
                } catch (err) {
                  setSave({ state: 'error', msg: classifyV3Error(err).title });
                }
              }}
            >
              Enregistrer sur le serveur
            </button>
          )}
          {editable && (
            <button
              type="button"
              className="v8-btn v8b-danger"
              onClick={async () => {
                if (!window.confirm(`Supprimer le tableau « ${board.title} » ? (les éléments épinglés ne sont que des références ; aucune donnée n’est supprimée)`)) return;
                await deleteBoard(board.id, origin as Origin);
                navigate('/v8/tableaux');
              }}
            >
              Supprimer
            </button>
          )}
        </div>
      </header>

      <p className="sr-only" aria-live="assertive" data-testid="announce">
        {announce}
      </p>

      {save.state === 'error' && (
        <div className="v8b-banner v8b-banner-err" role="alert">
          <strong>Modification non enregistrée.</strong> {save.msg}
          {conflict && (
            <button
              type="button"
              className="v8-btn"
              onClick={() => {
                latest.current = conflict;
                version.current = conflict.version ?? null;
                setBoard(conflict);
                setConflict(null);
                setSave({ state: 'idle' });
              }}
            >
              Recharger la version du serveur
            </button>
          )}
        </div>
      )}
      {origin === 'shared' && (
        <div className="v8b-banner" role="note">
          Tableau partagé par lien : lecture seule, rien n’est enregistré. Les valeurs affichées sont relues maintenant, avec leurs sources (elles peuvent différer de
          celles vues par l’auteur). « Garder une copie » le crée chez vous.
        </div>
      )}
      {undo && (
        <div className="v8b-banner v8b-banner-undo" role="status">
          « {undo.label} » retiré.{' '}
          <button type="button" className="v8-btn" onClick={doUndo}>
            Annuler <kbd>Ctrl</kbd>+<kbd>Z</kbd>
          </button>
        </div>
      )}

      {panel === 'share' && (
        <div className="v8b-panel" aria-label="Partager">
          <h2 className="v8b-h2">Partager</h2>
          <p>
            Ce lien contient le tableau lui-même (références des éléments, notes et règles) : quiconque l’ouvre voit un rendu en lecture seule, valeurs relues en direct.
            Il ne contient aucune valeur ni identifiant de session.
          </p>
          <div className="v8b-row">
            <label className="sr-only" htmlFor="v8b-share">
              Lien de partage
            </label>
            <input id="v8b-share" className="v8-input v8b-share-in" readOnly value={share} onFocus={(e) => e.currentTarget.select()} />
            <button
              type="button"
              className="v8-btn v8-btn-primary"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(share);
                  setCopied(true);
                } catch {
                  (document.getElementById('v8b-share') as HTMLInputElement | null)?.select();
                }
              }}
            >
              {copied ? 'Copié ✓' : 'Copier'}
            </button>
          </div>
          {origin === 'server' && <p className="v8b-meta muted">Le lien direct {v8Links.board(board.id)} ne s’ouvre que pour vous (tableau lié à votre session).</p>}
        </div>
      )}

      {panel === 'add' && editable && <AddPanel onAdd={addPin} onClose={() => setPanel('none')} />}

      {panel === 'rules' && editable && (
        <RulesPanel
          board={board}
          focusUid={watchUid}
          onAdd={(r) => {
            commit(addRule(board, r), 'Règle de veille ajoutée.');
            setAlertTick((t) => t + 1);
          }}
          onRemove={(id) => commit(removeRule(board, id), 'Règle supprimée.')}
          onClose={() => {
            setPanel('none');
            setWatchUid(null);
          }}
        />
      )}

      {board.rules.length > 0 && origin !== 'shared' && (
        <section className="v8b-section" aria-labelledby="v8b-alerts-h">
          <h2 id="v8b-alerts-h" className="v8b-h2">
            Alertes
          </h2>
          {alerts.status === 'loading' && <div className="v8b-skel" aria-hidden="true" />}
          {alerts.status === 'error' && (
            <p className={classifyV3Error(alerts.error).kind === 'unavailable' ? 'v8b-soft' : 'v8b-err'} role="status">
              {classifyV3Error(alerts.error).title} — alertes ({origin === 'server' ? 'service des tableaux' : 'fil des changements'}). Les règles restent enregistrées.
            </p>
          )}
          {alerts.status === 'ok' && <AlertList alerts={alerts.data.alerts} board={board} note={alerts.data.note} />}
        </section>
      )}

      {board.items.length === 0 ? (
        <div className="v8b-empty">
          <p>
            <strong>Tableau vide.</strong> Épinglez une entité, une série, une prévision ou une décision depuis n’importe quelle page (bouton « Épingler »), ou
            ajoutez-en ici.
          </p>
          {editable && (
            <div className="v8b-row">
              <button type="button" className="v8-btn v8-btn-primary" onClick={() => setPanel('add')}>
                Ajouter un élément <kbd>a</kbd>
              </button>
              <button type="button" className="v8-btn" onClick={addNote}>
                Écrire une note <kbd>n</kbd>
              </button>
            </div>
          )}
        </div>
      ) : (
        <div
          className="v8b-grid"
          ref={gridRef}
          role="list"
          aria-label={`Éléments du tableau (${board.items.length})`}
          onDragLeave={(e) => {
            if (!gridRef.current?.contains(e.relatedTarget as Node | null)) setDrag((d) => (d ? { ...d, over: d.from } : d));
          }}
        >
          {board.items.map((it, i) => (
            <div role="listitem" key={it.uid} className="v8b-cell">
              <ItemCard
                item={it}
                index={i}
                count={board.items.length}
                editable={editable}
                grabbed={grab?.uid === it.uid || drag?.from === i}
                dragOver={!!drag && drag.over === i && drag.from !== i}
                rules={board.rules.filter((r) => r.item_uid === it.uid)}
                alerts={alertList.filter((a) => a.item_uid === it.uid)}
                onHandleKey={onHandleKey}
                onRemove={removeAt}
                onWatch={(uid) => {
                  setWatchUid(uid);
                  setPanel('rules');
                }}
                onNote={(uid, md) => commit(updateItem(board, uid, { markdown: md } as Partial<PinItem>), 'Note enregistrée.')}
                onDragStart={(from) => setDrag({ from, over: from })}
                onDragOverCard={(over) => setDrag((d) => (d && d.over !== over ? { ...d, over } : d))}
                onDrop={() => {
                  if (drag) move(drag.from, drag.over);
                  setDrag(null);
                }}
                onDragEnd={() => setDrag(null)}
              />
            </div>
          ))}
        </div>
      )}
      <p className="v8b-keys muted" aria-hidden="true">
        <kbd>j</kbd>/<kbd>k</kbd> carte suivante/précédente · <kbd>Espace</kbd> sur ⠿ saisir puis flèches · <kbd>Alt</kbd>+flèches déplacer · <kbd>a</kbd> ajouter ·{' '}
        <kbd>n</kbd> note · <kbd>r</kbd> veille · <kbd>s</kbd> partager
      </p>
    </section>
  );
}

export function boardErrorText(err: unknown): string {
  return classifyV3Error(err).title;
}
