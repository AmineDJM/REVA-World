import { useState, type FormEvent } from 'react';
import { login } from '../api/client';
import { ApiError } from '../api/problem';
import type { SessionInfo } from '../api/types';

/** Private UI login. The session lives in an HttpOnly cookie; nothing is stored in localStorage. */
export function LoginPage({ onLogin }: { onLogin: (s: SessionInfo) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const s = await login(username, password);
      setPassword('');
      onLogin(s);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.kind === 'rate_limited') setError('Too many sign-in attempts. Wait a minute and retry.');
        else if (err.kind === 'unauthenticated') setError('Invalid username or password.');
        else if (err.kind === 'unreachable') setError('The REVA API is unreachable.');
        else setError(err.problem.detail ?? err.message);
      } else setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main id="main" className="login">
      <h1>REVA World</h1>
      <p className="muted">Private instance. Sign in to continue.</p>
      <form onSubmit={submit} aria-describedby={error ? 'login-error' : undefined}>
        <label>
          Username
          <input
            type="text"
            name="username"
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && (
          <p id="login-error" role="alert" className="field-error">
            {error}
          </p>
        )}
        <button type="submit" className="primary" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </main>
  );
}
