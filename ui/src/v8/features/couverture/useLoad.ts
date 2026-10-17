import { useEffect, useRef, useState } from 'react';

export interface Load<T> {
  status: 'idle' | 'loading' | 'ok' | 'error';
  data: T | null;
  error: unknown;
  reload: () => void;
}

/** Runs `fn` when `key` changes (null → idle); keeps the previous data while the next loads; drops stale answers. */
export function useLoad<T>(key: string | null, fn: () => Promise<T>): Load<T> {
  const [st, setSt] = useState<{ status: Load<T>['status']; data: T | null; error: unknown }>({ status: 'idle', data: null, error: null });
  const [nonce, setNonce] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (key === null) {
      setSt({ status: 'idle', data: null, error: null });
      return;
    }
    let live = true;
    setSt((s) => ({ status: 'loading', data: s.data, error: null }));
    fnRef.current().then(
      (data) => live && setSt({ status: 'ok', data, error: null }),
      (error: unknown) => live && setSt((s) => ({ status: 'error', data: s.data, error })),
    );
    return () => {
      live = false;
    };
  }, [key, nonce]);
  return { ...st, reload: () => setNonce((n) => n + 1) };
}
