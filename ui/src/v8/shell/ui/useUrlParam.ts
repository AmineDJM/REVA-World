import { useCallback } from 'react';
import { useSearchParams } from 'react-router';

/**
 * One URL per state: a string state stored in the query string (?key=value). Setting the default
 * value removes the parameter. `replace` (default true) avoids flooding history while typing; pass
 * false for discrete navigations that back/forward should step through.
 */
export function useUrlParam(key: string, fallback = ''): [string, (v: string, opts?: { replace?: boolean }) => void] {
  const [params, setParams] = useSearchParams();
  const value = params.get(key) ?? fallback;
  const set = useCallback(
    (v: string, opts?: { replace?: boolean }) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (v === fallback || v === '') next.delete(key);
          else next.set(key, v);
          return next;
        },
        { replace: opts?.replace ?? true },
      );
    },
    [key, fallback, setParams],
  );
  return [value, set];
}
