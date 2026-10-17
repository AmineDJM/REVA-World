/**
 * Public pinning API of the Tableaux feature — the ONE cross-feature import allowed in V8
 * (docs/dev/v8_v8boards.md § "pinItem"). Any feature may do:
 *
 *   import { pinItem } from '../tableaux/pin';
 *   const r = await pinItem({ kind: 'series', concept: 'macro.cpi_inflation', geo: 'FRA', label: 'Inflation, France' });
 *   // r.added === false when the same thing was already pinned on that board (no duplicate)
 *
 * Pins are references (ids, concept + country, question id, decision ref, Markdown note), never values: a board
 * reads every value live, with its source. The target board is `opts.boardId`, else the last board used, else the
 * most recent, else a new board « Mes épingles ». Boards live on the server (/v1/v8/boards) when the route is
 * deployed, otherwise in this browser. A `v8:boards-changed` window event is emitted after every pin.
 *
 * Only this file (and the types it re-exports) is the public surface; other files of the feature are private.
 */
import { addItem, PinItem as PinItemSchema, type PinItem } from './model';
import { BOARDS_EVENT, createBoard, lastBoardId, listBoards, loadBoard, rememberBoard, saveBoard, type BoardListEntry } from './store';

export type { PinItem } from './model';
export type { BoardListEntry } from './store';
export { BOARDS_EVENT } from './store';

export interface PinResult {
  boardId: string;
  boardTitle: string;
  /** false when an identical pin was already on the board (nothing changed). */
  added: boolean;
  /** Where the board lives: on the server, or only in this browser. */
  origin: 'server' | 'local';
  /** V8 URL of the board. */
  href: string;
}

export const DEFAULT_BOARD_TITLE = 'Mes épingles';

/** Validate a pin; throws a TypeError naming what is wrong (nothing is pinned half-formed). */
export function validatePin(item: unknown): PinItem {
  const r = PinItemSchema.safeParse(item);
  if (!r.success) throw new TypeError(`Épingle refusée : ${r.error.issues.map((i) => `${i.path.join('.') || 'élément'} ${i.message}`).join(' ; ')}`);
  return r.data;
}

export async function pinItem(item: PinItem, opts: { boardId?: string } = {}): Promise<PinResult> {
  const pin = validatePin(item);
  let targetId = opts.boardId ?? lastBoardId();
  let entry: { board: Awaited<ReturnType<typeof loadBoard>>['board']; origin: 'server' | 'local' } | null = null;
  if (targetId) {
    try {
      entry = await loadBoard(targetId);
    } catch {
      if (opts.boardId) throw new Error(`Tableau ${opts.boardId} introuvable.`);
      entry = null;
    }
  }
  if (!entry) {
    const list = await listBoards();
    const first = list.boards[0];
    entry = first ? await loadBoard(first.id) : await createBoard(DEFAULT_BOARD_TITLE, list.mode === 'local');
  }
  targetId = entry.board.id;
  const { board, added } = addItem(entry.board, pin);
  const saved = added ? await saveBoard({ ...board, version: entry.board.version ?? null }, entry.origin) : entry.board;
  rememberBoard(targetId);
  return { boardId: saved.id, boardTitle: saved.title, added, origin: entry.origin, href: `/v8/tableaux/${encodeURIComponent(saved.id)}` };
}

/** Boards for a "pin to…" menu or a preview (server + local), most recently updated first. */
export async function listBoardsForPin(signal?: AbortSignal): Promise<{ boards: BoardListEntry[]; mode: 'server' | 'local'; note: string | null }> {
  const r = await listBoards(signal);
  return { boards: r.boards, mode: r.mode, note: r.serverNote };
}

/** Subscribe to board changes (pins, edits); returns the unsubscribe function. */
export function onPinned(fn: () => void): () => void {
  if (typeof window === 'undefined') return () => {};
  window.addEventListener(BOARDS_EVENT, fn);
  return () => window.removeEventListener(BOARDS_EVENT, fn);
}
