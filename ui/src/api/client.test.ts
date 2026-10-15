import { afterEach, describe, expect, it, vi } from 'vitest';
import { cancelJob, createScenario, getBeliefs, logout, reviewProposal, startDiscovery, startReplay, startSimulation } from './client';
import { ApiError } from './problem';
import { onUnauthorized, readCookie, request } from './http';
import { mockFetch } from '../test/fetchMock';
import type { ScenarioSpec } from './types';

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

afterEach(() => {
  vi.unstubAllGlobals();
});

function setCsrf(v: string) {
  document.cookie = `reva_csrf=${encodeURIComponent(v)}; path=/`;
}

describe('http client', () => {
  it('reads the CSRF cookie', () => {
    expect(readCookie('reva_csrf', 'a=1; reva_csrf=tok%2Fen; b=2')).toBe('tok/en');
    expect(readCookie('reva_csrf', 'a=1')).toBeUndefined();
  });

  it('sends same-origin credentials and no CSRF header on safe methods', async () => {
    setCsrf('csrf-123');
    const { calls } = mockFetch({ 'GET /v1/beliefs': { body: { data: [] } } });
    await getBeliefs({ subject_id: 'FRA', concept: undefined, limit: 5 });
    expect(calls).toHaveLength(1);
    expect(calls[0]!.credentials).toBe('same-origin');
    expect(calls[0]!.headers.get('X-CSRF-Token')).toBeNull();
    expect(calls[0]!.headers.get('Idempotency-Key')).toBeNull();
    expect(calls[0]!.url).toBe('/v1/beliefs?subject_id=FRA&limit=5');
  });

  it('sends the CSRF header from the reva_csrf cookie on unsafe methods', async () => {
    setCsrf('csrf-abc');
    const { calls } = mockFetch({ 'DELETE /v1/session': { status: 204 }, 'POST /v1/jobs/': { body: { job_id: 'j', status: 'cancel_requested' } } });
    await logout();
    await cancelJob('j1');
    expect(calls.map((c) => c.headers.get('X-CSRF-Token'))).toEqual(['csrf-abc', 'csrf-abc']);
    expect(calls.every((c) => c.credentials === 'same-origin')).toBe(true);
  });

  it('adds a fresh UUID Idempotency-Key to every work-starting POST', async () => {
    setCsrf('t');
    const accepted = { job_id: 'j', job_type: 'x', status: 'queued', status_url: '/v1/jobs/j' };
    const { calls } = mockFetch({
      'POST /v1/simulations': { status: 202, body: accepted },
      'POST /v1/replay': { status: 202, body: accepted },
      'POST /v1/discovery': { status: 202, body: accepted },
      'POST /v1/scenarios': { status: 201, body: { scenario_id: 's' } },
      'POST /v1/proposals/': { status: 201, body: { review_id: 'r', proposal_id: 'p', decision: 'reject', reviewer: 'human:admin' } },
    });
    await startSimulation({ scenario_id: 's', fidelity: 'L1' });
    await startReplay({ mode: 'operational_as_known', cutoff: '2026-01-01T00:00:00Z', horizon_years: 1 });
    await startDiscovery({ catalogue: 'eurostat', requirement_id: 'R-POP-JAN-AGESEX', mode: 'fixture' });
    await createScenario({} as ScenarioSpec);
    await reviewProposal('p', 'reject', 'no');
    const keys = calls.map((c) => c.headers.get('Idempotency-Key'));
    expect(keys.every((k) => k !== null && UUID_V4.test(k))).toBe(true);
    expect(new Set(keys).size).toBe(keys.length);
    expect(calls.every((c) => c.headers.get('X-CSRF-Token') === 't')).toBe(true);
  });

  it('reuses a caller-supplied Idempotency-Key for retries', async () => {
    const { calls } = mockFetch({ 'POST /v1/simulations': { status: 202, body: { job_id: 'j' } } });
    await startSimulation({ scenario_id: 's', fidelity: 'L1' }, 'fixed-key-123');
    await startSimulation({ scenario_id: 's', fidelity: 'L1' }, 'fixed-key-123');
    expect(calls.map((c) => c.headers.get('Idempotency-Key'))).toEqual(['fixed-key-123', 'fixed-key-123']);
  });

  it('parses RFC 9457 problem details into ApiError', async () => {
    mockFetch({
      'POST /v1/simulations': {
        status: 422,
        body: { type: 'https://reva.invalid/problems/missing_input', title: 'missing input', status: 422, detail: 'DZA missing', code: 'missing_input', details: { missing: [{ country: 'DZA' }] } },
      },
    });
    const err = await startSimulation({ scenario_id: 's', fidelity: 'L1' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect(e.status).toBe(422);
    expect(e.kind).toBe('unprocessable');
    expect(e.code).toBe('missing_input');
    expect(e.problem.detail).toBe('DZA missing');
    expect(e.problem.details).toEqual({ missing: [{ country: 'DZA' }] });
  });

  it('reports a network failure as unreachable, not as empty data', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))));
    const err = await getBeliefs().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).kind).toBe('unreachable');
  });

  it('notifies listeners on 401', async () => {
    mockFetch({ 'GET /v1/beliefs': { status: 401, body: { title: 'unauthenticated', status: 401, detail: 'authentication required', code: 'unauthenticated' } } });
    const fn = vi.fn();
    const off = onUnauthorized(fn);
    await expect(request('GET', '/v1/beliefs')).rejects.toBeInstanceOf(ApiError);
    off();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('never writes tokens to web storage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    mockFetch({ 'POST /v1/session': { body: { username: 'admin', principal: 'human:admin', auth_method: 'session', scopes: [], csrf_token: 'x' } } });
    const { login } = await import('./client');
    await login('admin', 'secret-password');
    expect(setItem).not.toHaveBeenCalled();
    setItem.mockRestore();
  });
});
