/**
 * One board store over two backends: the server (/v1/v8/boards, persisted per principal) when the route is
 * deployed, else this browser (localStorage, labelled "local"). Local boards stay listed next to server ones
 * and can be uploaded once the server answers. Every mutation emits `BOARDS_EVENT` so open views refresh.
 */
import { ApiError } from '../../../api/problem';
import {
  conflictBoard,
  createServerBoard,
  deleteServerBoard,
  getServerBoard,
  listServerBoards,
  saveServerBoard,
  serviceAbsent,
  type ServerBoard,
} from './api';
import { readLocalBoards, writeLocalBoards } from './local';
import { emptyBoard, type Board } from './model';

export const BOARDS_EVENT = 'v8:boards-changed';
const LAST_KEY = 'reva.v8.boards.last';

export type StoredBoard = Board & { version?: number | null };
export type Origin = 'server' | 'local';

export interface BoardListEntry {
  id: string;
  title: string;
  n_items: number;
  n_rules: number;
  updated_at: string;
  origin: Origin;
}

export interface BoardList {
  /** "server": the board service answered; "local": it is absent (or unreachable) and boards live in this browser. */
  mode: Origin;
  boards: BoardListEntry[];
  /** Why the server is not used (absent route, unreachable, refused), French. */
  serverNote: string | null;
}

export class BoardConflict extends Error {
  readonly current: ServerBoard | null;
  constructor(current: ServerBoard | null) {
    super('Ce tableau a été modifié ailleurs entre-temps.');
    this.name = 'BoardConflict';
    this.current = current;
  }
}

export function notifyBoards(detail?: unknown): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent(BOARDS_EVENT, { detail }));
}

export function onBoardsChanged(fn: () => void): () => void {
  if (typeof window === 'undefined') return () => {};
  window.addEventListener(BOARDS_EVENT, fn);
  return () => window.removeEventListener(BOARDS_EVENT, fn);
}

export function lastBoardId(): string | null {
  try {
    return window.localStorage.getItem(LAST_KEY);
  } catch {
    return null;
  }
}

export function rememberBoard(id: string): void {
  try {
    window.localStorage.setItem(LAST_KEY, id);
  } catch {
    /* per-viewer convenience only */
  }
}

function serverNoteOf(err: unknown): string {
  if (serviceAbsent(err)) return 'Service des tableaux absent de ce serveur : les tableaux sont gardés dans ce navigateur.';
  if (err instanceof ApiError && err.kind === 'unreachable') return 'API injoignable : les tableaux sont gardés dans ce navigateur.';
  if (err instanceof ApiError && (err.status === 401 || err.status === 403)) return 'Session sans accès au service des tableaux : tableaux gardés dans ce navigateur.';
  return `Service des tableaux en erreur (${err instanceof ApiError ? `HTTP ${err.status}` : 'réponse non conforme'}) : tableaux gardés dans ce navigateur.`;
}

function localEntry(b: Board): BoardListEntry {
  return { id: b.id, title: b.title, n_items: b.items.length, n_rules: b.rules.length, updated_at: b.updated_at, origin: 'local' };
}

export async function listBoards(signal?: AbortSignal): Promise<BoardList> {
  const local = readLocalBoards().map(localEntry);
  try {
    const srv = await listServerBoards(signal);
    const ids = new Set(srv.map((b) => b.id));
    const boards: BoardListEntry[] = [
      ...srv.map((b) => ({ id: b.id, title: b.title, n_items: b.n_items, n_rules: b.n_rules, updated_at: b.updated_at, origin: 'server' as const })),
      ...local.filter((b) => !ids.has(b.id)),
    ];
    return { mode: 'server', boards: sortByUpdate(boards), serverNote: null };
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    return { mode: 'local', boards: sortByUpdate(local), serverNote: serverNoteOf(err) };
  }
}

function sortByUpdate(bs: BoardListEntry[]): BoardListEntry[] {
  return bs.slice().sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

export async function loadBoard(id: string, signal?: AbortSignal): Promise<{ board: StoredBoard; origin: Origin }> {
  const local = readLocalBoards().find((b) => b.id === id);
  if (local) return { board: local, origin: 'local' };
  const board = await getServerBoard(id, signal);
  return { board, origin: 'server' };
}

export async function createBoard(title: string, preferLocal = false): Promise<{ board: StoredBoard; origin: Origin }> {
  const draft = emptyBoard(title);
  if (!preferLocal) {
    try {
      const board = await createServerBoard({ id: draft.id, title: draft.title, items: [], rules: [] });
      notifyBoards();
      return { board, origin: 'server' };
    } catch (err) {
      if (!serviceAbsent(err) && !(err instanceof ApiError && err.kind === 'unreachable')) throw err;
    }
  }
  const all = readLocalBoards();
  if (!writeLocalBoards([draft, ...all])) throw new Error('Stockage du navigateur indisponible : impossible de garder un tableau local.');
  notifyBoards();
  return { board: draft, origin: 'local' };
}

/** Save the whole board. Server: optimistic concurrency (BoardConflict carries the current server board). */
export async function saveBoard(board: StoredBoard, origin: Origin): Promise<StoredBoard> {
  if (origin === 'server') {
    try {
      const saved = await saveServerBoard({ id: board.id, title: board.title, items: board.items, rules: board.rules, version: board.version ?? null });
      notifyBoards(board.id);
      return saved;
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) throw new BoardConflict(conflictBoard(err));
      throw err;
    }
  }
  const all = readLocalBoards();
  const i = all.findIndex((b) => b.id === board.id);
  const plain: Board = { id: board.id, title: board.title, items: board.items, rules: board.rules, created_at: board.created_at, updated_at: board.updated_at };
  if (i >= 0) all[i] = plain;
  else all.unshift(plain);
  if (!writeLocalBoards(all)) throw new Error('Stockage du navigateur indisponible : modification non gardée.');
  notifyBoards(board.id);
  return plain;
}

export async function deleteBoard(id: string, origin: Origin): Promise<void> {
  if (origin === 'server') await deleteServerBoard(id);
  else writeLocalBoards(readLocalBoards().filter((b) => b.id !== id));
  notifyBoards();
}

/** Copy a local (or shared) board to the server under the same id; the local copy is then removed. */
export async function uploadBoard(board: Board): Promise<StoredBoard> {
  const saved = await createServerBoard({ id: board.id, title: board.title, items: board.items, rules: board.rules });
  writeLocalBoards(readLocalBoards().filter((b) => b.id !== board.id));
  notifyBoards();
  return saved;
}

/** Keep a copy of a shared (read-only) board in this browser or on the server, under a new id. */
export async function importBoard(board: Board, mode: Origin): Promise<{ board: StoredBoard; origin: Origin }> {
  const now = new Date().toISOString();
  const fresh = { ...emptyBoard(board.title, now), items: board.items, rules: board.rules };
  if (mode === 'server') {
    try {
      const saved = await createServerBoard({ id: fresh.id, title: fresh.title, items: fresh.items, rules: fresh.rules });
      notifyBoards();
      return { board: saved, origin: 'server' };
    } catch (err) {
      if (!serviceAbsent(err)) throw err;
    }
  }
  if (!writeLocalBoards([fresh, ...readLocalBoards()])) throw new Error('Stockage du navigateur indisponible.');
  notifyBoards();
  return { board: fresh, origin: 'local' };
}
