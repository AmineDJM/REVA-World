/** Entry chunk of /v8 (lazy-loaded by App.tsx): the live feature registry + the frame. */
import { featureForPath } from './discovery';
import { prefetchElement } from './prefetch';
import { DISCOVERY } from './registry';
import { V8App } from './V8App';

// Start loading the landing route's feature code now, while the frame renders.
if (typeof window !== 'undefined') {
  const f = featureForPath(DISCOVERY.features, window.location.pathname);
  if (f) prefetchElement(f.element);
}

export default function V8Root({ username, onLogout }: { username?: string | null; onLogout?: () => void }) {
  return <V8App features={DISCOVERY.features} problems={DISCOVERY.problems} username={username} onLogout={onLogout} />;
}
