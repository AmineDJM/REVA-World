/** Entry chunk of /v8 (lazy-loaded by App.tsx): the live feature registry + the frame. */
import { DISCOVERY } from './registry';
import { V8App } from './V8App';

export default function V8Root({ username, onLogout }: { username?: string | null; onLogout?: () => void }) {
  return <V8App features={DISCOVERY.features} problems={DISCOVERY.problems} username={username} onLogout={onLogout} />;
}
