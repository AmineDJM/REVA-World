import { useEffect, useRef, useState } from 'react';

export type Async<T> = { status: 'idle' } | { status: 'loading'; prev: T | null } | { status: 'ok'; data: T } | { status: 'error'; error: unknown; prev: T | null };

/**
 * Runs `fn` when `key` changes (null key → idle). Keeps the previous data while the next one loads (no layout
 * jump during animation), drops stale answers.
 */
export function useAsync<T>(key: string | null, fn: () => Promise<T>): Async<T> & { reload: () => void } {
  const [state, setState] = useState<Async<T>>({ status: 'idle' });
  const [nonce, setNonce] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const last = useRef<T | null>(null);
  useEffect(() => {
    if (key === null) {
      setState({ status: 'idle' });
      return;
    }
    let live = true;
    setState({ status: 'loading', prev: last.current });
    fnRef
      .current()
      .then((data) => {
        if (!live) return;
        last.current = data;
        setState({ status: 'ok', data });
      })
      .catch((error: unknown) => {
        if (live) setState({ status: 'error', error, prev: last.current });
      });
    return () => {
      live = false;
    };
  }, [key, nonce]);
  return { ...state, reload: () => setNonce((n) => n + 1) };
}

export function dataOf<T>(a: Async<T>): T | null {
  if (a.status === 'ok') return a.data;
  if (a.status === 'loading' || a.status === 'error') return a.prev;
  return null;
}

/** Current theme (light/dark), following the shell toggle and the system preference. */
export function useDark(): boolean {
  const read = () => {
    const t = document.documentElement.dataset.theme;
    if (t === 'dark') return true;
    if (t === 'light') return false;
    try {
      return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
    } catch {
      return false;
    }
  };
  const [dark, setDark] = useState(read);
  useEffect(() => {
    const on = () => setDark(read());
    window.addEventListener('v8:theme', on);
    let mq: MediaQueryList | null = null;
    try {
      mq = window.matchMedia?.('(prefers-color-scheme: dark)') ?? null;
      mq?.addEventListener?.('change', on);
    } catch {
      mq = null;
    }
    const obs = new MutationObserver(on);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => {
      window.removeEventListener('v8:theme', on);
      mq?.removeEventListener?.('change', on);
      obs.disconnect();
    };
  }, []);
  return dark;
}
