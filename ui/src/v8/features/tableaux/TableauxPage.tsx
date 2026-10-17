/**
 * Route /v8/tableaux (index of boards), /v8/tableaux/:id (one board), /v8/tableaux/partage#b=… (shared board,
 * read-only render of the JSON carried in the fragment). The feature path is "tableaux/*"; the board id is read
 * from the pathname so the page works whatever the shell's nesting.
 */
import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router';
import { v8Links } from '../../contract';
import { classifyV3Error } from '../../../v3/api/common';
import { useApi } from '../../../lib/useApi';
import { BoardView } from './BoardView';
import { decodeShare, SHARE_ID } from './model';
import { createBoard, listBoards, loadBoard, onBoardsChanged, type BoardListEntry } from './store';
import './tableaux.css';

function boardIdFrom(pathname: string): string | null {
  const m = /\/tableaux\/([^/]+)\/?$/.exec(pathname);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1]!);
  } catch {
    return m[1]!;
  }
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('fr-FR', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function BoardsIndex() {
  const navigate = useNavigate();
  const [tick, setTick] = useState(0);
  useEffect(() => onBoardsChanged(() => setTick((t) => t + 1)), []);
  const st = useApi((s) => listBoards(s), [tick]);
  const [title, setTitle] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const newRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'n' || e.key === 'c') {
        e.preventDefault();
        newRef.current?.focus();
      } else if (e.key === 'j' || e.key === 'k') {
        const links = Array.from(listRef.current?.querySelectorAll<HTMLAnchorElement>('a.v8b-board-link') ?? []);
        if (!links.length) return;
        const cur = links.indexOf(document.activeElement as HTMLAnchorElement);
        e.preventDefault();
        links[cur < 0 ? 0 : Math.max(0, Math.min(links.length - 1, cur + (e.key === 'j' ? 1 : -1)))]!.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const create = async () => {
    setErr(null);
    try {
      const r = await createBoard(title || 'Nouveau tableau', st.status === 'ok' && st.data.mode === 'local');
      navigate(v8Links.board(r.board.id));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section className="v8b-index" aria-labelledby="v8b-index-h">
      <header className="v8b-board-head">
        <div>
          <h1 id="v8b-index-h" className="v8b-h1">
            Tableaux
          </h1>
          <p className="v8b-board-sub">Épinglez entités, séries, prévisions, décisions et notes ; surveillez-les (nouvelles données, révisions, résolutions, seuils).</p>
        </div>
        <form
          className="v8b-row"
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <label htmlFor="v8b-new" className="sr-only">
            Titre du nouveau tableau
          </label>
          <input id="v8b-new" ref={newRef} className="v8-input" placeholder="Nouveau tableau…" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
          <button type="submit" className="v8-btn v8-btn-primary">
            Créer <kbd>n</kbd>
          </button>
        </form>
      </header>
      {err && (
        <p className="v8b-err" role="alert">
          {err}
        </p>
      )}
      {st.status === 'loading' && <div className="v8b-skel v8b-skel-tall" aria-hidden="true" />}
      {st.status === 'error' && (
        <p className="v8b-err" role="alert">
          {classifyV3Error(st.error).title} — liste des tableaux.
        </p>
      )}
      {st.status === 'ok' && (
        <>
          {st.data.serverNote && (
            <p className="v8b-banner" role="note">
              {st.data.serverNote}
            </p>
          )}
          {st.data.boards.length === 0 ? (
            <div className="v8b-empty">
              <p>
                <strong>Aucun tableau.</strong> Créez-en un ci-dessus, ou épinglez un élément depuis une fiche, une carte, une prévision ou une décision : un tableau « Mes
                épingles » est alors créé.
              </p>
            </div>
          ) : (
            <ul className="v8b-board-list" ref={listRef}>
              {st.data.boards.map((b: BoardListEntry) => (
                <li key={`${b.origin}:${b.id}`}>
                  <Link className="v8b-board-link" to={v8Links.board(b.id)}>
                    <span className="v8b-board-name">{b.title}</span>
                    <span className="v8b-board-counts v8-num">
                      {b.n_items} élément{b.n_items > 1 ? 's' : ''} · {b.n_rules} règle{b.n_rules > 1 ? 's' : ''}
                    </span>
                    <span className={`v8-badge v8b-origin v8b-origin-${b.origin}`}>{b.origin === 'server' ? 'serveur' : 'local'}</span>
                    <span className="v8b-board-date">modifié {fmtDate(b.updated_at)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function SharedBoard() {
  const { hash } = useLocation();
  const r = decodeShare(hash);
  if (!r.ok)
    return (
      <section className="v8b-board">
        <h1 className="v8b-h1">Tableau partagé</h1>
        <p className="v8b-err" role="alert">
          Lien de partage refusé : {r.reason}. Rien n’est affiché plutôt qu’un tableau partiel.
        </p>
        <p>
          <Link to="/v8/tableaux">Retour aux tableaux</Link>
        </p>
      </section>
    );
  return <BoardView key={hash} initial={r.board} origin="shared" />;
}

function OneBoard({ id }: { id: string }) {
  const st = useApi((s) => loadBoard(id, s), [id]);
  if (st.status === 'loading' || st.status === 'idle') return <div className="v8b-skel v8b-skel-tall" aria-hidden="true" />;
  if (st.status === 'error') {
    const info = classifyV3Error(st.error);
    return (
      <section className="v8b-board">
        <h1 className="v8b-h1">Tableau introuvable</h1>
        <p className={info.kind === 'not_found' || info.kind === 'unavailable' ? 'v8b-soft' : 'v8b-err'} role="status">
          {info.kind === 'not_found'
            ? `Aucun tableau « ${id} » ici : il n’existe pas, appartient à une autre session, ou a été gardé dans un autre navigateur.`
            : info.kind === 'unavailable'
              ? `Le tableau « ${id} » n’est pas dans ce navigateur et le service des tableaux est absent de ce serveur.`
              : `${info.title} — tableau « ${id} ».`}
        </p>
        <p>
          <Link to="/v8/tableaux">Tous les tableaux</Link>
        </p>
      </section>
    );
  }
  return <BoardView key={`${st.data.origin}:${id}`} initial={st.data.board} origin={st.data.origin} />;
}

export default function TableauxPage() {
  const { pathname } = useLocation();
  const id = boardIdFrom(pathname);
  return <div className="v8-page v8b-page">{id === null ? <BoardsIndex /> : id === SHARE_ID ? <SharedBoard /> : <OneBoard id={id} />}</div>;
}
