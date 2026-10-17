import { useEffect, useRef, useState } from 'react';
import type { PaletteHit, PaletteProvider } from '../../contract';

export const DEBOUNCE_MS = 120;

export interface ProviderState {
  hits: PaletteHit[];
  pending: boolean;
  /** Providers that failed for the current query (count; errors are not shown as results). */
  failed: number;
  /** The query the hits belong to. */
  query: string;
}

/**
 * Runs every async provider for the query: debounced (120 ms), the previous run aborted (AbortController)
 * as soon as the query changes, results merged progressively in provider order; stale results never shown.
 */
export function useProviders(query: string, providers: readonly PaletteProvider[], debounceMs = DEBOUNCE_MS): ProviderState {
  const [state, setState] = useState<ProviderState>({ hits: [], pending: false, failed: 0, query: '' });
  const ctrl = useRef<AbortController | null>(null);
  useEffect(() => {
    ctrl.current?.abort();
    ctrl.current = null;
    const q = query.trim();
    if (!q || providers.length === 0) {
      setState({ hits: [], pending: false, failed: 0, query: q });
      return;
    }
    setState((s) => ({ ...s, pending: true }));
    const timer = setTimeout(() => {
      const c = new AbortController();
      ctrl.current = c;
      const parts: (PaletteHit[] | null)[] = providers.map(() => null);
      let done = 0;
      let failed = 0;
      const publish = () => {
        if (c.signal.aborted) return;
        setState({ hits: parts.flatMap((p) => p ?? []), pending: done < providers.length, failed, query: q });
      };
      providers.forEach((p, i) => {
        let run: Promise<PaletteHit[]>;
        try {
          run = p(q, c.signal);
        } catch (e) {
          run = Promise.reject(e);
        }
        run.then(
          (hits) => {
            parts[i] = Array.isArray(hits) ? hits : [];
            done += 1;
            publish();
          },
          (e: unknown) => {
            if (e instanceof DOMException && e.name === 'AbortError') return;
            done += 1;
            failed += 1;
            publish();
          },
        );
      });
    }, debounceMs);
    return () => {
      clearTimeout(timer);
      ctrl.current?.abort();
    };
  }, [query, providers, debounceMs]);
  return state;
}
