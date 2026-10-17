import { act, fireEvent, render, screen, within } from '@testing-library/react';
import type { PaletteHit, PaletteProvider } from '../../contract';
import { collectFeatures } from '../discovery';
import { buildGroups, commandItems, navItems } from './engine';
import { fuzzyScore } from './fuzzy';
import { Palette } from './Palette';
import { RECENT_KEY, readRecents } from './recents';

const El = () => null;
const { features } = collectFeatures({
  '../features/monde/index.tsx': {
    feature: {
      id: 'monde',
      title: 'Monde',
      path: 'monde',
      order: 30,
      goKey: 'm',
      element: El,
      commands: [
        {
          id: 'cmp',
          label: 'Comparer des pays',
          keywords: ['comparer', 'compare'],
          run: (q: string) => {
            const rest = q.replace(/^\S+\s*/, '').trim();
            return rest ? `/v8/comparer?q=${encodeURIComponent(rest)}` : '/v8/comparer';
          },
        },
        { id: 'never', label: 'Jamais', keywords: ['jamais'], run: () => null },
      ],
    },
  },
  '../features/scores/index.tsx': { feature: { id: 'scores', title: 'Scores', path: 'scores', order: 40, goKey: 's', element: El } },
});

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

const hit = (label: string, group: string, href = `/v8/fiche/x/${label}`): PaletteHit => ({ id: label, label, href, group });

function setup(providers: PaletteProvider[] = [], extra: Partial<Parameters<typeof Palette>[0]> = {}) {
  const navigate = vi.fn();
  const openNewTab = vi.fn();
  const onClose = vi.fn();
  const utils = render(<Palette open onClose={onClose} features={features} providers={providers} builtins={[]} navigate={navigate} openNewTab={openNewTab} {...extra} />);
  const input = screen.getByRole('combobox');
  return { ...utils, navigate, openNewTab, onClose, input };
}

describe('fuzzy + engine', () => {
  it('matches accents-insensitively, prefers substrings and word starts', () => {
    expect(fuzzyScore('petrole', 'Pétrole brut')).toBeGreaterThan(1000);
    expect(fuzzyScore('brut', 'Pétrole brut')!).toBeGreaterThan(fuzzyScore('rut', 'Pétrole brut')!);
    expect(fuzzyScore('pbr', 'Pétrole brut')).not.toBeNull();
    expect(fuzzyScore('xyz', 'Pétrole brut')).toBeNull();
  });

  it('commands match by leading keyword and run with the whole query; null = hidden', () => {
    const items = commandItems(features, [], 'comparer FRA DEU inflation');
    expect(items[0]!.href).toBe('/v8/comparer?q=FRA%20DEU%20inflation');
    expect(commandItems(features, [], 'jamais').length).toBe(0);
  });

  it('groups: empty query = récents, aller à, commandes; typed = strong commands, nav, provider groups (dedup, capped)', () => {
    const empty = buildGroups({ query: '', recents: [hit('France', 'Pays')], nav: navItems(features, ''), commands: commandItems(features, [], ''), hits: [] });
    expect(empty.map((g) => g.group)).toEqual(['Récents', 'Aller à', 'Commandes']);
    const hits = [hit('A', 'Entités'), hit('B', 'Indicateurs'), hit('A', 'Entités'), ...Array.from({ length: 12 }, (_, i) => hit(`E${i}`, 'Entités'))];
    const typed = buildGroups({ query: 'mon', recents: [], nav: navItems(features, 'mon'), commands: commandItems(features, [], 'mon'), hits });
    expect(typed.map((g) => g.group)).toEqual(['Aller à', 'Entités', 'Indicateurs']);
    expect(typed[1]!.items.length).toBe(8);
  });
});

describe('Palette', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    localStorage.clear();
  });
  afterEach(() => vi.useRealTimers());

  it('keyboard: ↓/↑ move, Enter navigates and records a recent, Ctrl-Enter opens a new tab', async () => {
    const { input, navigate, openNewTab } = setup();
    // empty query: nav items Monde, Scores
    const opts = () => screen.getAllByRole('option');
    expect(opts()[0]).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(opts()[1]).toHaveAttribute('aria-selected', 'true');
    expect(input.getAttribute('aria-activedescendant')).toBe(opts()[1]!.id);
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    fireEvent.keyDown(input, { key: 'ArrowUp' }); // wraps
    expect(opts()[opts().length - 1]).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(navigate).toHaveBeenCalledWith('/v8/monde');
    expect(readRecents()[0]!.href).toBe('/v8/monde');
    fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true });
    expect(openNewTab).toHaveBeenCalledWith('/v8/monde');
  });

  it('debounces providers (120 ms), aborts the previous query, never shows stale results', async () => {
    const calls: { q: string; signal: AbortSignal; d: ReturnType<typeof deferred<PaletteHit[]>> }[] = [];
    const provider: PaletteProvider = (q, signal) => {
      const d = deferred<PaletteHit[]>();
      calls.push({ q, signal, d });
      return d.promise;
    };
    const { input } = setup([provider]);
    fireEvent.change(input, { target: { value: 'f' } });
    fireEvent.change(input, { target: { value: 'fr' } });
    await act(async () => {
      vi.advanceTimersByTime(119);
    });
    expect(calls.length).toBe(0); // still debouncing
    await act(async () => {
      vi.advanceTimersByTime(2);
    });
    expect(calls.map((c) => c.q)).toEqual(['fr']);
    fireEvent.change(input, { target: { value: 'fra' } });
    expect(calls[0]!.signal.aborted).toBe(true);
    await act(async () => {
      calls[0]!.d.resolve([hit('Stale', 'Entités')]);
      vi.advanceTimersByTime(130);
    });
    expect(calls.map((c) => c.q)).toEqual(['fr', 'fra']);
    await act(async () => {
      calls[1]!.d.resolve([hit('France', 'Pays & régions'), hit('Fraise', 'Entités')]);
    });
    expect(screen.queryByText('Stale')).toBeNull();
    const groups = screen.getAllByRole('group');
    const names = groups.map((g) => within(g).getAllByText(/./)[0]!.textContent);
    expect(names).toEqual(['Pays & régions', 'Entités']);
    expect(screen.getByRole('option', { name: /France/ })).toBeInTheDocument();
  });

  it('a failing provider does not hide the others; empty result says so', async () => {
    const ok: PaletteProvider = async () => [hit('Brésil', 'Pays & régions')];
    const ko: PaletteProvider = async () => {
      throw new Error('500');
    };
    const { input } = setup([ko, ok]);
    fireEvent.change(input, { target: { value: 'bré' } });
    await act(async () => {
      vi.advanceTimersByTime(130);
    });
    expect(screen.getByRole('option', { name: /Brésil/ })).toBeInTheDocument();
    fireEvent.change(input, { target: { value: 'zzzzqq' } });
    const none: PaletteProvider = async () => [];
    void none;
    await act(async () => {
      vi.advanceTimersByTime(130);
    });
    expect(screen.getByText(/Aucun résultat pour « zzzzqq »/)).toBeInTheDocument();
  });

  it('recents survive a broken localStorage value and Escape closes', () => {
    localStorage.setItem(RECENT_KEY, '{not json');
    const { input, onClose } = setup();
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});
