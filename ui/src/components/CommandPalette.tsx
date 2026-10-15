import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useNavigate } from 'react-router';
import { getBeliefReleases, getEntities, getEntity, getJobs, getOntology, getScenarios } from '../api/client';
import { COUNTRY_CODES } from '../api/types';
import { formatDateTime, shortId } from '../lib/format';
import { KIND_LABEL, KIND_PLURAL, rankItems, type PaletteItem, type PaletteKind } from '../lib/palette';

export interface PaletteScreen {
  to: string;
  label: string;
}

interface SourceState {
  items: PaletteItem[];
  errors: Partial<Record<PaletteKind, string>>;
  loading: boolean;
}

/** Load the searchable catalogue once (countries, concepts, releases, runs). Each source fails on its own. */
export async function loadPaletteItems(signal?: AbortSignal): Promise<{ items: PaletteItem[]; errors: Partial<Record<PaletteKind, string>> }> {
  const errors: Partial<Record<PaletteKind, string>> = {};
  const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));
  const items: PaletteItem[] = [];

  const [countries, profiles, ontology, releases, jobs, scenarios] = await Promise.allSettled([
    getEntities({ kind: 'entity.country', limit: 1000 }, signal),
    Promise.all(COUNTRY_CODES.map(async (c) => ({ iso: c, id: (await getEntity(c, signal)).data.id }))),
    getOntology(undefined, signal),
    getBeliefReleases({ limit: 50 }, signal),
    getJobs({ job_type: 'simulation', limit: 100 }, signal),
    getScenarios({ limit: 500 }, signal),
  ]);

  const profileIso = new Map<string, string>();
  if (profiles.status === 'fulfilled') for (const p of profiles.value) profileIso.set(p.id, p.iso);
  if (countries.status === 'fulfilled') {
    const seen = new Set<string>();
    for (const e of countries.value.data) {
      if (seen.has(e.id)) continue;
      seen.add(e.id);
      const iso = profileIso.get(e.id);
      items.push({
        id: e.id,
        kind: 'country',
        title: e.label,
        subtitle: iso ? `${iso} · country profile` : 'world map (focus)',
        keywords: iso ? [iso] : [],
        href: iso ? `/country/${iso}` : `/map?focus=${encodeURIComponent(e.id)}`,
      });
    }
  } else errors.country = msg(countries.reason);

  if (ontology.status === 'fulfilled') {
    for (const c of ontology.value.data.concepts) {
      const label = (c as unknown as { labels?: Record<string, string> }).labels?.en;
      items.push({
        id: `${c.concept_id}@${c.version}`,
        kind: 'concept',
        title: label || c.concept_id,
        subtitle: `${c.concept_id}@${c.version}`,
        keywords: [c.concept_id],
        href: `/evidence?concept=${encodeURIComponent(c.concept_id)}`,
      });
    }
  } else errors.concept = msg(ontology.reason);

  if (releases.status === 'fulfilled') {
    for (const r of releases.value.data) {
      items.push({
        id: r.id,
        kind: 'release',
        title: `Release #${r.sequence ?? '—'}`,
        subtitle: `cutoff ${formatDateTime(r.knowledge_cutoff)} · ${r.origin === 'test_fixture' ? 'TEST FIXTURE' : r.origin} · ${shortId(r.id)}`,
        keywords: [r.id, String(r.sequence ?? ''), r.origin ?? ''],
        href: `/simulation?release=${encodeURIComponent(r.id)}`,
      });
    }
  } else errors.release = msg(releases.reason);

  if (jobs.status === 'fulfilled') {
    const byId = new Map(scenarios.status === 'fulfilled' ? scenarios.value.data.map((s) => [s.id, s]) : []);
    for (const j of jobs.value.data) {
      const sc = byId.get(String(j.payload?.scenario_id ?? ''));
      const model = sc ? String(sc.spec.model_key ?? '') : '';
      items.push({
        id: j.id,
        kind: 'run',
        title: sc?.title ? `${sc.title}` : `Run ${shortId(j.id)}`,
        subtitle: `${shortId(j.id)} · ${model || 'model not listed'} · ${String(j.payload?.fidelity ?? '')} · ${j.status} · ${formatDateTime(j.created_at)}`,
        keywords: [j.id, model, j.status, String(j.payload?.fidelity ?? '')],
        href: `/runs/${encodeURIComponent(j.id)}`,
      });
    }
  } else errors.run = msg(jobs.reason);

  return { items, errors };
}

/**
 * ⌘K / Ctrl+K command palette: deterministic search over screens, countries, concepts, runs and
 * releases (no LLM). Combobox + listbox pattern; Escape closes and restores focus.
 */
export function CommandPalette({ screens }: { screens: PaletteScreen[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const [src, setSrc] = useState<SourceState>({ items: [], errors: {}, loading: false });
  const loadedRef = useRef(false);
  const opener = useRef<HTMLElement | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const listId = useId();
  const titleId = useId();

  const screenItems: PaletteItem[] = useMemo(
    () => screens.map((s) => ({ id: s.to, kind: 'screen' as const, title: s.label, subtitle: s.to, keywords: [s.to.replace(/^\//, '')], href: s.to })),
    [screens],
  );
  const results = useMemo(() => rankItems([...screenItems, ...src.items], query, 40), [screenItems, src.items, query]);

  const show = useCallback(() => {
    opener.current = document.activeElement as HTMLElement | null;
    setOpen(true);
    setQuery('');
    setActive(0);
    if (!loadedRef.current) {
      loadedRef.current = true;
      setSrc((s) => ({ ...s, loading: true }));
      loadPaletteItems()
        .then((r) => setSrc({ items: r.items, errors: r.errors, loading: false }))
        .catch((e: unknown) => {
          loadedRef.current = false;
          setSrc({ items: [], errors: { screen: e instanceof Error ? e.message : String(e) }, loading: false });
        });
    }
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    const el = opener.current;
    if (el && typeof el.focus === 'function') setTimeout(() => el.focus(), 0);
  }, []);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (open) close();
        else show();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, show, close]);

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  useEffect(() => setActive(0), [query]);

  const go = (item: PaletteItem | undefined) => {
    if (!item) return;
    setOpen(false);
    navigate(item.href);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => (results.length ? (a + 1) % results.length : 0));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => (results.length ? (a - 1 + results.length) % results.length : 0));
    } else if (e.key === 'Home' && e.ctrlKey) {
      setActive(0);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      go(results[active]);
    } else if (e.key === 'Tab') {
      // Keep focus inside the dialog: the input and the close button.
      const focusables = Array.from((e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>('input, button'));
      const i = focusables.indexOf(document.activeElement as HTMLElement);
      const next = e.shiftKey ? (i <= 0 ? focusables.length - 1 : i - 1) : (i + 1) % focusables.length;
      e.preventDefault();
      focusables[next]?.focus();
    }
  };

  const optionId = (i: number) => `${listId}-opt-${i}`;
  const errorKinds = Object.entries(src.errors) as [PaletteKind, string][];

  return (
    <>
      <button type="button" className="palette-trigger" onClick={show} aria-haspopup="dialog" aria-keyshortcuts="Meta+K Control+K">
        Search <kbd>⌘K</kbd>
      </button>
      {open && (
        <div className="palette-backdrop" onMouseDown={(e) => e.target === e.currentTarget && close()}>
          <div className="palette" role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={onKeyDown} data-testid="command-palette">
            <h2 id={titleId} className="sr-only">
              Search countries, concepts, runs, releases and screens
            </h2>
            <div className="palette-input-row">
              <input
                ref={input}
                type="text"
                role="combobox"
                aria-expanded="true"
                aria-controls={listId}
                aria-autocomplete="list"
                aria-activedescendant={results[active] ? optionId(active) : undefined}
                aria-label="Search countries, concepts, runs, releases and screens"
                placeholder="Search countries, concepts, runs, releases…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
              <button type="button" onClick={close}>
                Close
              </button>
            </div>
            <p className="palette-hint field-hint">
              Deterministic text search (no language model). ↑↓ to move, Enter to open, Esc to close.
            </p>
            {src.loading && (
              <p className="state state-loading" role="status">
                Loading catalogue…
              </p>
            )}
            {errorKinds.length > 0 && (
              <p className="field-error" role="status">
                Not searchable right now: {errorKinds.map(([k, m]) => `${KIND_PLURAL[k] ?? k} (${m})`).join('; ')}.
              </p>
            )}
            <ul id={listId} role="listbox" aria-label="Results" className="palette-list">
              {results.map((r, i) => (
                <li
                  key={`${r.kind}:${r.id}`}
                  id={optionId(i)}
                  role="option"
                  aria-selected={i === active}
                  className={i === active ? 'active' : undefined}
                  onMouseMove={() => setActive(i)}
                  onClick={() => go(r)}
                >
                  <span className={`palette-kind kind-${r.kind}`}>{KIND_LABEL[r.kind]}</span>
                  <span className="palette-title">{r.title}</span>
                  {r.subtitle && <span className="palette-sub">{r.subtitle}</span>}
                </li>
              ))}
            </ul>
            {!src.loading && results.length === 0 && (
              <p className="muted" role="status">
                Nothing matches “{query}”.
              </p>
            )}
            <p className="sr-only" role="status" aria-live="polite">
              {results.length} result{results.length === 1 ? '' : 's'}
            </p>
          </div>
        </div>
      )}
    </>
  );
}
