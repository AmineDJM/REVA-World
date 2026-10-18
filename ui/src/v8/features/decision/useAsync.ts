import { useEffect, useState } from 'react';

export type Async<T> = { status: 'idle' } | { status: 'loading' } | { status: 'ok'; data: T } | { status: 'error'; error: unknown };

/** Run `fn` when `key` changes (null = idle); aborts the previous call; never sets state after unmount. */
export function useAsync<T>(key: string | null, fn: (signal: AbortSignal) => Promise<T>): [Async<T>, () => void] {
  const [state, setState] = useState<Async<T>>({ status: 'idle' });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (key === null) {
      setState({ status: 'idle' });
      return;
    }
    const ctl = new AbortController();
    setState({ status: 'loading' });
    fn(ctl.signal).then(
      (data) => {
        if (!ctl.signal.aborted) setState({ status: 'ok', data });
      },
      (error: unknown) => {
        if (!ctl.signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) setState({ status: 'error', error });
      },
    );
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, nonce]);
  return [state, () => setNonce((n) => n + 1)];
}
