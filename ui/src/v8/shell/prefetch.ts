/**
 * Prefetch a feature's code on nav hover/focus or palette selection, so the route switch is instant.
 * React.lazy has no public preload: initialising the lazy payload starts the import (React throws the
 * pending promise, which we swallow). Anything else is a no-op.
 */
const started = new WeakSet<object>();

export function prefetchElement(el: unknown): void {
  const lazy = el as { $$typeof?: symbol; _payload?: unknown; _init?: (p: unknown) => unknown } | null;
  if (!lazy || typeof lazy !== 'object' || typeof lazy._init !== 'function' || started.has(lazy)) return;
  if (lazy.$$typeof !== Symbol.for('react.lazy')) return;
  started.add(lazy);
  try {
    lazy._init(lazy._payload);
  } catch (thrown) {
    const t = thrown as { then?: (a: () => void, b: () => void) => void } | null;
    if (t && typeof t.then === 'function') t.then(() => undefined, () => undefined);
  }
}
