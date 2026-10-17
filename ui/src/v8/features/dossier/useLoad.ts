import { useEffect, useRef, useState } from 'react';

export interface Load<T> {
  status: 'idle' | 'loading' | 'ok' | 'error';
  data: T | null;
  error: unknown;
  reload: () => void;
}

/** Runs `fn` when `key` changes (null → idle); drops stale answers; keeps the previous data while the next loads
 * only for the same subject (a new key starts empty, so one brief never shows under another's title). */
export function useLoad<T>(key: string | null, fn: () => Promise<T>): Load<T> {
  const [st, setSt] = useState<{ key: string | null; status: Load<T>['status']; data: T | null; error: unknown }>({ key: null, status: 'idle', data: null, error: null });
  const [nonce, setNonce] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (key === null) {
      setSt({ key, status: 'idle', data: null, error: null });
      return;
    }
    let live = true;
    setSt((s) => ({ key, status: 'loading', data: s.key === key ? s.data : null, error: null }));
    fnRef.current().then(
      (data) => live && setSt({ key, status: 'ok', data, error: null }),
      (error: unknown) => live && setSt((s) => ({ key, status: 'error', data: s.key === key ? s.data : null, error })),
    );
    return () => {
      live = false;
    };
  }, [key, nonce]);
  return { status: st.status, data: st.data, error: st.error, reload: () => setNonce((n) => n + 1) };
}
