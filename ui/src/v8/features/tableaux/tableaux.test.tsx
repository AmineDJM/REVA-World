import { act, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { envelope, mockFetch } from '../../../test/fetchMock';
import FORECAST from './__fixtures__/forecast_question_fra_cpi_2026.json';
import SERIES from './__fixtures__/headline_series_fra_inflation.json';
import { BoardView } from './BoardView';
import { feature } from './index';
import { readLocalBoards, writeLocalBoards } from './local';
import { Markdown, safeHref } from './Markdown';
import {
  addItem,
  addRule,
  decodeShare,
  emptyBoard,
  encodeShare,
  moveItem,
  normaliseDecimalInput,
  removeItem,
  ruleProblem,
  rulesFor,
  shareHref,
  type Board,
  type BoardItem,
} from './model';
import { pinItem, validatePin } from './pin';
import { evaluateLocal, hitToPin, urlToPin } from './rules';
import TableauxPage from './TableauxPage';

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 20_000 });

/** The REVA API's answer for a route that is not deployed (src/reva/api/errors.py). */
const NO_ROUTE = { status: 404, body: { type: 'https://reva.invalid/problems/not_found', title: 'not found', status: 404, detail: 'no such endpoint', code: 'not_found' } };

beforeEach(() => {
  window.localStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

function boardWith(items: Parameters<typeof addItem>[1][]): Board {
  let b = emptyBoard('Énergie', '2026-10-17T10:00:00Z');
  for (const p of items) b = addItem(b, p, '2026-10-17T10:00:00Z').board;
  return b;
}

const SERIES_PIN = { kind: 'series' as const, concept: 'reva.economy.inflation_cpi', geo: 'FRA', label: 'Inflation · France', headline: true };
const FORECAST_PIN = { kind: 'forecast' as const, questionId: 'fq_455b1860c774831e20563f8c', label: 'Inflation France 2026' };
const NOTE_PIN = { kind: 'note' as const, markdown: '# Hypothèses\n- **Brent** au-dessus de 80 $' };

// ------------------------------------------------------------------ model

describe('board model', () => {
  it('adds pins without duplicates (notes are always added) and removes their rules with them', () => {
    let b = boardWith([SERIES_PIN]);
    const again = addItem(b, { ...SERIES_PIN, label: 'autre libellé' });
    expect(again.added).toBe(false);
    expect(again.board.items).toHaveLength(1);
    b = addItem(b, NOTE_PIN).board;
    b = addItem(b, NOTE_PIN).board;
    expect(b.items.map((i) => i.kind)).toEqual(['series', 'note', 'note']);
    const uid = b.items[0]!.uid;
    b = addRule(b, { kind: 'threshold', item_uid: uid, op: '>', value: '2.5' });
    expect(b.rules).toHaveLength(1);
    b = removeItem(b, uid);
    expect(b.rules).toHaveLength(0);
  });

  it('moves items (clamped, pure)', () => {
    const xs = ['a', 'b', 'c', 'd'];
    expect(moveItem(xs, 0, 2)).toEqual(['b', 'c', 'a', 'd']);
    expect(moveItem(xs, 3, -5)).toEqual(['d', 'a', 'b', 'c']);
    expect(moveItem(xs, 1, 99)).toEqual(['a', 'c', 'd', 'b']);
    expect(xs).toEqual(['a', 'b', 'c', 'd']);
  });

  it('offers only meaningful rules and validates thresholds as decimals', () => {
    expect(rulesFor('series')).toEqual(['new_data', 'revision', 'threshold']);
    expect(rulesFor('forecast')).toEqual(['resolution']);
    expect(rulesFor('note')).toEqual([]);
    const b = boardWith([SERIES_PIN, FORECAST_PIN]);
    const [s, f] = b.items as [BoardItem, BoardItem];
    expect(ruleProblem({ kind: 'threshold', op: '>', value: '2.5' }, s)).toBeNull();
    expect(ruleProblem({ kind: 'threshold', op: '>', value: 'deux' }, s)).toMatch(/décimal/);
    expect(ruleProblem({ kind: 'threshold', op: '>', value: '2.5' }, f)).toMatch(/sans objet/);
    expect(ruleProblem({ kind: 'resolution' }, f)).toBeNull();
    expect(normaliseDecimalInput(' 2,5 ')).toBe('2.5');
  });

  it('round-trips a board through the share URL (UTF-8) and refuses anything else', () => {
    const b = boardWith([SERIES_PIN, NOTE_PIN]);
    const r = decodeShare(`#b=${encodeShare(b)}`);
    expect(r.ok && r.board).toEqual(b);
    expect(shareHref(b)).toMatch(/^\/v8\/tableaux\/partage#b=[A-Za-z0-9_-]+$/);
    expect(decodeShare('#b=%%%')).toEqual({ ok: false, reason: expect.stringMatching(/illisible/) });
    const forged = btoa(JSON.stringify({ v: 1, board: { ...b, items: [{ kind: 'script', uid: 'x', added_at: 'y' }] } }));
    expect(decodeShare(forged).ok).toBe(false);
    expect(decodeShare('').ok).toBe(false);
  });
});

// ------------------------------------------------------------------ markdown

describe('Markdown notes', () => {
  it('renders the supported subset as elements and never injects HTML or unsafe links', () => {
    const { container } = render(
      <Markdown source={'# Titre\nTexte **gras** et *italique* `code`\n- un\n- deux\n[ok](/v8/monde) [mal](javascript:alert(1)) <img src=x onerror=alert(1)>'} />,
    );
    expect(container.querySelector('h4')?.textContent).toBe('Titre');
    expect(container.querySelector('strong')?.textContent).toBe('gras');
    expect(container.querySelectorAll('li')).toHaveLength(2);
    expect(screen.getByRole('link', { name: 'ok' })).toHaveAttribute('href', '/v8/monde');
    expect(screen.queryByRole('link', { name: 'mal' })).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>');
    expect(safeHref('https://imf.org')).toBe('https://imf.org');
    expect(safeHref('data:text/html,x')).toBeNull();
  });
});

// ------------------------------------------------------------------ pins, search, local alerts

describe('pinItem (public cross-feature API)', () => {
  it('pins into a new local board « Mes épingles » when the board service is absent, without duplicates', async () => {
    mockFetch({ 'GET /v1/v8/boards': NO_ROUTE, 'POST /v1/v8/boards': NO_ROUTE });
    const events: Event[] = [];
    window.addEventListener('v8:boards-changed', (e) => events.push(e));
    const r1 = await pinItem(SERIES_PIN);
    expect(r1).toMatchObject({ added: true, origin: 'local', boardTitle: 'Mes épingles' });
    expect(r1.href).toBe(`/v8/tableaux/${r1.boardId}`);
    const r2 = await pinItem({ ...SERIES_PIN });
    expect(r2).toMatchObject({ added: false, boardId: r1.boardId });
    const r3 = await pinItem(FORECAST_PIN);
    expect(r3.boardId).toBe(r1.boardId);
    expect(readLocalBoards()[0]!.items.map((i) => i.kind)).toEqual(['series', 'forecast']);
    expect(events.length).toBeGreaterThan(0);
  });

  it('pins into a server board with its version (optimistic concurrency)', async () => {
    const server = { ...boardWith([]), id: 'b_srv', version: 4 };
    const { calls } = mockFetch({
      'GET /v1/v8/boards/b_srv': { body: envelope(server) },
      'GET /v1/v8/boards': { body: envelope({ boards: [{ id: 'b_srv', title: 'Énergie', n_items: 0, n_rules: 0, updated_at: server.updated_at, version: 4 }] }) },
      'POST /v1/v8/boards/b_srv': (c) => ({ body: envelope({ ...(c.body as object), id: 'b_srv', created_at: server.created_at, updated_at: server.updated_at, version: 5 }) }),
    });
    const r = await pinItem(FORECAST_PIN);
    expect(r).toMatchObject({ boardId: 'b_srv', origin: 'server', added: true });
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.body).toMatchObject({ version: 4, items: [expect.objectContaining({ kind: 'forecast', questionId: FORECAST_PIN.questionId })] });
  });

  it('refuses a malformed pin with a readable reason', () => {
    expect(() => validatePin({ kind: 'series', concept: 'x' })).toThrow(/Épingle refusée/);
    expect(() => validatePin({ kind: 'value', value: 3 })).toThrow(TypeError);
  });
});

describe('add from search or link; local rule evaluation', () => {
  it('maps hits and pasted V8 links to pins', () => {
    expect(hitToPin({ id: 'fq_455b1860c774831e20563f8c', kind: 'question', label: 'Q', href: '/v8/fiche/question/fq_' })).toMatchObject({ kind: 'forecast' });
    expect(hitToPin({ id: 'reva.economy.inflation_cpi', kind: 'indicator', label: 'Inflation', href: '/v8/monde?concept=x&headline=1' })).toBeNull();
    expect(hitToPin({ id: 'reva.economy.inflation_cpi', kind: 'indicator', label: 'Inflation', href: '/v8/monde?concept=x&headline=1' }, 'fra')).toMatchObject({
      kind: 'series',
      geo: 'FRA',
      headline: true,
    });
    expect(hitToPin({ id: 'FRA', kind: 'country', label: 'France', href: '/v8/fiche/country/FRA' })).toMatchObject({ kind: 'entity', entityKind: 'country', id: 'FRA' });
    expect(urlToPin('/v8/fiche/drug/chembl%3ACHEMBL941')).toMatchObject({ kind: 'entity', entityKind: 'drug', id: 'chembl:CHEMBL941' });
    expect(urlToPin('https://reva.example/v8/decision?ref=ecb_plus_50bp_2027q1')).toMatchObject({ kind: 'decision', ref: 'ecb_plus_50bp_2027q1' });
    expect(urlToPin('/v8/monde?concept=c&geo=deu')).toMatchObject({ kind: 'series', geo: 'DEU' });
    expect(urlToPin('fq_455b1860c774831e20563f8c')).toMatchObject({ kind: 'forecast' });
    expect(urlToPin('bonjour')).toBeNull();
  });

  it('fires only what the feed decides and says what it cannot evaluate', () => {
    let b = boardWith([FORECAST_PIN, { ...SERIES_PIN, table: 'imf.weo' }]);
    const [f, s] = b.items as [BoardItem, BoardItem];
    b = addRule(b, { kind: 'resolution', item_uid: f.uid });
    b = addRule(b, { kind: 'new_data', item_uid: s.uid });
    b = addRule(b, { kind: 'threshold', item_uid: s.uid, op: '>', value: '2' });
    const alerts = evaluateLocal(b, [
      { id: 'c1', kind: 'question_resolved', at: '2026-10-17T09:00:00Z', title: 'Résolue', links: [], refs: [{ type: 'question', id: FORECAST_PIN.questionId }] },
      { id: 'c2', kind: 'ingest', at: '2026-10-17T08:00:00Z', title: 'IMF WEO', links: [], refs: [{ type: 'table', id: 'atlas.imf.weo' }] },
      { id: 'c3', kind: 'ingest', at: '2026-10-17T07:00:00Z', title: 'Autre', links: [], refs: [{ type: 'table', id: 'atlas.wb.wdi' }] },
    ]);
    expect(alerts.filter((a) => a.status === 'fired').map((a) => a.change?.id)).toEqual(['c1', 'c2']);
    expect(alerts.find((a) => a.kind === 'threshold')).toMatchObject({ status: 'not_evaluable', reason: expect.stringMatching(/serveur/) });
  });
});

// ------------------------------------------------------------------ views

function renderBoard(board: Board, origin: 'local' | 'server' | 'shared' = 'local') {
  return render(
    <MemoryRouter initialEntries={['/v8/tableaux/x']}>
      <BoardView initial={board} origin={origin} />
    </MemoryRouter>,
  );
}

function liveRoutes() {
  return mockFetch({
    'GET /v1/v8/boards': NO_ROUTE,
    'GET /v1/v8/changes': NO_ROUTE,
    'GET /v1/atlas/headline/series': { body: SERIES },
    'GET /v1/forecasts/questions/': { body: FORECAST },
  });
}

describe('BoardView', () => {
  it('renders live items with honesty: observed value + source, projections labelled, sealed forecasts with the official one attributed', async () => {
    liveRoutes();
    const b = boardWith([SERIES_PIN, FORECAST_PIN, NOTE_PIN]);
    writeLocalBoards([b]);
    renderBoard(b);
    const cards = screen.getAllByTestId('board-item');
    expect(cards).toHaveLength(3);
    // series: latest OBSERVED value (2025), never the 2026–2031 projections
    const series = within(cards[0]!);
    expect(await series.findByText('0,93')).toBeInTheDocument();
    expect(series.getByText(/2025 · observé/)).toBeInTheDocument();
    expect(series.getByText(/projection 2026–2031/)).toBeInTheDocument();
    expect(series.getByText(/changement de source/)).toBeInTheDocument();
    expect(series.getByRole('img', { name: /projections \(pointillés\)/ })).toBeInTheDocument();
    // forecast: sealed, pending, IMF first and attributed
    const fc = within(cards[1]!);
    expect(await fc.findByText(/sceau vérifié/)).toBeInTheDocument();
    expect(fc.getByText('en attente')).toBeInTheDocument();
    const rows = fc.getAllByRole('row');
    expect(rows[1]).toHaveTextContent(/FMI — WEO/);
    expect(rows[1]).toHaveTextContent(/officiel · attribué/);
    expect(rows[1]).toHaveTextContent('2,41');
    // note
    // note: its first heading is the card title, not repeated in the body
    expect(within(cards[2]!).getAllByRole('heading', { name: 'Hypothèses' })).toHaveLength(1);
    expect(within(cards[2]!).getByText('Brent')).toBeInTheDocument();
  });

  it('reorders with the keyboard (Space, arrows, Space), announces it and saves locally', async () => {
    liveRoutes();
    const b = boardWith([NOTE_PIN, { kind: 'note', markdown: 'deuxième' }, { kind: 'note', markdown: 'troisième' }]);
    writeLocalBoards([b]);
    renderBoard(b);
    const handles = screen.getAllByRole('button', { name: /^Déplacer/ });
    handles[0]!.focus();
    fireEvent.keyDown(handles[0]!, { key: ' ' });
    expect(screen.getByTestId('announce')).toHaveTextContent(/saisi, position 1 sur 3/);
    fireEvent.keyDown(handles[0]!, { key: 'ArrowDown' });
    fireEvent.keyDown(screen.getAllByRole('button', { name: /^Déplacer/ })[1]!, { key: 'ArrowDown' });
    expect(screen.getByTestId('announce')).toHaveTextContent(/position 3 sur 3/);
    fireEvent.keyDown(screen.getAllByRole('button', { name: /^Déplacer/ })[2]!, { key: ' ' });
    await waitFor(() => expect(readLocalBoards()[0]!.items.map((i) => (i.kind === 'note' ? i.markdown : ''))).toEqual(['deuxième', 'troisième', NOTE_PIN.markdown]));
    // Alt+ArrowUp moves without grabbing
    fireEvent.keyDown(screen.getAllByRole('button', { name: /^Déplacer/ })[2]!, { key: 'ArrowUp', altKey: true });
    await waitFor(() => expect(readLocalBoards()[0]!.items.map((i) => (i.kind === 'note' ? i.markdown : ''))).toEqual(['deuxième', NOTE_PIN.markdown, 'troisième']));
  });

  it('cancels a keyboard move with Escape', () => {
    liveRoutes();
    const b = boardWith([{ kind: 'note', markdown: 'A' }, { kind: 'note', markdown: 'B' }]);
    renderBoard(b);
    const h = screen.getAllByRole('button', { name: /^Déplacer/ });
    fireEvent.keyDown(h[0]!, { key: 'Enter' });
    fireEvent.keyDown(h[0]!, { key: 'ArrowRight' });
    fireEvent.keyDown(screen.getAllByRole('button', { name: /^Déplacer/ })[1]!, { key: 'Escape' });
    expect(screen.getByTestId('announce')).toHaveTextContent(/annulé/);
    expect(screen.getAllByTestId('board-item')[0]).toHaveTextContent('A');
  });

  it('removes an item with undo (Ctrl+Z)', async () => {
    liveRoutes();
    const b = boardWith([{ kind: 'note', markdown: 'A' }, { kind: 'note', markdown: 'B' }]);
    renderBoard(b);
    await userEvent.click(screen.getAllByRole('button', { name: /^Retirer/ })[0]!);
    expect(screen.getAllByTestId('board-item')).toHaveLength(1);
    fireEvent.keyDown(window, { key: 'z', ctrlKey: true });
    expect(screen.getAllByTestId('board-item')).toHaveLength(2);
  });

  it('adds a threshold rule on a series and refuses a non-decimal one', async () => {
    liveRoutes();
    const b = boardWith([SERIES_PIN]);
    writeLocalBoards([b]);
    renderBoard(b);
    await userEvent.click(screen.getByRole('button', { name: /Surveiller/ }));
    await userEvent.selectOptions(screen.getByLabelText('Quand'), 'threshold');
    await userEvent.type(screen.getByLabelText(/Seuil/), 'beaucoup');
    await userEvent.click(screen.getByRole('button', { name: 'Ajouter la règle' }));
    expect(screen.getByRole('alert')).toHaveTextContent(/décimal/);
    await userEvent.clear(screen.getByLabelText(/Seuil/));
    await userEvent.type(screen.getByLabelText(/Seuil/), '2,5');
    await userEvent.click(screen.getByRole('button', { name: 'Ajouter la règle' }));
    await waitFor(() => expect(readLocalBoards()[0]!.rules).toEqual([expect.objectContaining({ kind: 'threshold', op: '>', value: '2.5' })]));
    // local board: the threshold is honestly "not evaluable here"
    expect(await screen.findByText(/évalué seulement par le serveur/)).toBeInTheDocument();
  });

  it('renders a shared board read-only (no handles, no remove) with its notice', () => {
    liveRoutes();
    renderBoard(boardWith([NOTE_PIN]), 'shared');
    expect(screen.queryByRole('button', { name: /^Déplacer/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Retirer/ })).toBeNull();
    expect(screen.getByText(/lecture seule, rien n’est enregistré/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Garder une copie' })).toBeInTheDocument();
  });

  it('reloads the server version after a 409 version conflict', async () => {
    const server = { ...boardWith([NOTE_PIN]), id: 'b_srv', version: 2 };
    const current = { ...server, title: 'Renommé ailleurs', version: 3 };
    mockFetch({
      'GET /v1/v8/boards': { body: envelope({ boards: [] }) },
      'POST /v1/v8/boards/b_srv': { status: 409, body: { title: 'conflict', status: 409, code: 'version_conflict', detail: current } },
    });
    renderBoard(server, 'server');
    await userEvent.click(screen.getByRole('button', { name: /^Note/ }));
    expect(await screen.findByText(/Modification non enregistrée/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Recharger la version du serveur' }));
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Renommé ailleurs');
  });
});

describe('TableauxPage routes', () => {
  function at(url: string) {
    return render(
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/v8/tableaux/*" element={<TableauxPage />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it('lists local boards and says why they are local', async () => {
    mockFetch({ 'GET /v1/v8/boards': NO_ROUTE });
    writeLocalBoards([boardWith([NOTE_PIN])]);
    at('/v8/tableaux');
    expect(await screen.findByText(/Service des tableaux absent de ce serveur/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Énergie/ })).toHaveAttribute('href', expect.stringMatching(/^\/v8\/tableaux\/b_/));
  });

  it('creates a board (n focuses the title field)', async () => {
    mockFetch({ 'GET /v1/v8/boards': NO_ROUTE, 'POST /v1/v8/boards': NO_ROUTE });
    at('/v8/tableaux');
    await screen.findByText(/Aucun tableau/);
    fireEvent.keyDown(window, { key: 'n' });
    expect(screen.getByPlaceholderText('Nouveau tableau…')).toHaveFocus();
    await userEvent.type(screen.getByPlaceholderText('Nouveau tableau…'), 'Pétrole{Enter}');
    expect(await screen.findByRole('heading', { level: 1, name: /Pétrole/ })).toBeInTheDocument();
    expect(readLocalBoards()[0]!.title).toBe('Pétrole');
  });

  it('renders a shared link and refuses a forged one', async () => {
    liveRoutes();
    const b = boardWith([NOTE_PIN]);
    const { unmount } = at(shareHref(b));
    expect(await screen.findByText(/lecture seule, rien n’est enregistré/)).toBeInTheDocument();
    unmount();
    at('/v8/tableaux/partage#b=e30');
    expect(screen.getByRole('alert')).toHaveTextContent(/Lien de partage refusé/);
  });

  it('says honestly when a board is not found', async () => {
    mockFetch({ 'GET /v1/v8/boards/': { status: 404, body: { title: 'not found', status: 404, detail: 'board not found', code: 'not_found' } } });
    at('/v8/tableaux/b_inconnu');
    expect(await screen.findByText(/Aucun tableau « b_inconnu » ici/)).toBeInTheDocument();
  });
});

describe('feature contract', () => {
  it('exports a valid feature with palette search over boards', async () => {
    expect(feature).toMatchObject({ id: 'tableaux', path: 'tableaux', order: 70, goKey: 't' });
    mockFetch({ 'GET /v1/v8/boards': NO_ROUTE });
    writeLocalBoards([boardWith([NOTE_PIN])]);
    let hits: Awaited<ReturnType<NonNullable<typeof feature.search>>> = [];
    await act(async () => {
      hits = await feature.search!('ener', new AbortController().signal);
    });
    expect(hits[0]).toMatchObject({ label: 'Énergie', group: 'Tableaux' });
  });
});
