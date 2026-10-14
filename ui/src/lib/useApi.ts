import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';

export type ApiState<T> =
  | { status: 'loading'; data: T | undefined; error: undefined; reload: () => void }
  | { status: 'ok'; data: T; error: undefined; reload: () => void }
  | { status: 'error'; data: T | undefined; error: unknown; reload: () => void }
  | { status: 'idle'; data: undefined; error: undefined; reload: () => void };

/**
 * Fetch on mount / dependency change with abort on unmount. Pass `enabled=false`
 * to hold the request (e.g. until a prerequisite id is known).
 */
export function useApi<T>(fn: (signal: AbortSignal) => Promise<T>, deps: DependencyList, enabled = true): ApiState<T> {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<{ status: 'loading' | 'ok' | 'error' | 'idle'; data?: T; error?: unknown }>({
    status: enabled ? 'loading' : 'idle',
  });
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) {
      setState({ status: 'idle' });
      return;
    }
    const ctrl = new AbortController();
    setState((s) => ({ status: 'loading', data: s.data }));
    fnRef
      .current(ctrl.signal)
      .then((data) => {
        if (!ctrl.signal.aborted) setState({ status: 'ok', data });
      })
      .catch((error: unknown) => {
        if (ctrl.signal.aborted) return;
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setState((s) => ({ status: 'error', data: s.data, error }));
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, enabled]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload } as ApiState<T>;
}

export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}
