/**
 * Browser-local board storage: used only when the server has no board service (V8API /v1/v8/boards absent or
 * refusing). Every board kept here is labelled "local" in the UI (lives in this browser only). Corrupt or
 * foreign data is dropped item by item through the zod schema, never repaired by guessing.
 */
import { z } from 'zod';
import { Board } from './model';

const KEY = 'reva.v8.boards.v1';

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function readLocalBoards(): Board[] {
  const st = storage();
  if (!st) return [];
  try {
    const raw = st.getItem(KEY);
    if (!raw) return [];
    const arr = z.array(z.unknown()).safeParse(JSON.parse(raw));
    if (!arr.success) return [];
    const out: Board[] = [];
    for (const b of arr.data) {
      const r = Board.safeParse(b);
      if (r.success) out.push(r.data);
    }
    return out;
  } catch {
    return [];
  }
}

export function writeLocalBoards(boards: Board[]): boolean {
  const st = storage();
  if (!st) return false;
  try {
    st.setItem(KEY, JSON.stringify(boards));
    return true;
  } catch {
    return false;
  }
}

export function localStorageAvailable(): boolean {
  const st = storage();
  if (!st) return false;
  try {
    const k = `${KEY}.probe`;
    st.setItem(k, '1');
    st.removeItem(k);
    return true;
  } catch {
    return false;
  }
}
