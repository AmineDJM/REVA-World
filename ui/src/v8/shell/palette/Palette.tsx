import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import type { PaletteHit, PaletteProvider } from '../../contract';
import type { DiscoveredFeature } from '../discovery';
import { KeyHint } from '../ui/KeyHint';
import { buildGroups, commandItems, flatten, navItems, type BuiltinAction, type PaletteItem } from './engine';
import { highlight } from './fuzzy';
import { pushRecent, readRecents } from './recents';
import { useProviders } from './useProviders';

export interface PaletteProps {
  open: boolean;
  onClose: () => void;
  features: readonly DiscoveredFeature[];
  providers: readonly PaletteProvider[];
  builtins: readonly BuiltinAction[];
  /** Navigate inside the app (react-router). */
  navigate: (href: string) => void;
  /** Open in a new tab (Ctrl/⌘-Enter, Ctrl/⌘-click). */
  openNewTab?: (href: string) => void;
  /** Called when an item becomes active (prefetch its feature). */
  onActive?: (href: string | null) => void;
  /** Footer note (search backend). */
  footnote?: string;
  initialQuery?: string;
}

function Label({ text, query }: { text: string; query: string }) {
  const h = highlight(text, query);
  if (!h) return <>{text}</>;
  return (
    <>
      {h[0]}
      <mark>{h[1]}</mark>
      {h[2]}
    </>
  );
}

/** Ctrl/⌘-K palette: commands, navigation, async search; full keyboard navigation. */
export function Palette(props: PaletteProps) {
  if (!props.open) return null;
  return <PaletteDialog {...props} />;
}

function PaletteDialog({ onClose, features, providers, builtins, navigate, openNewTab, onActive, footnote, initialQuery = '' }: PaletteProps) {
  const [query, setQuery] = useState(initialQuery);
  const [active, setActive] = useState(0);
  const [recents, setRecents] = useState<PaletteHit[]>(readRecents);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const restore = useRef<Element | null>(typeof document !== 'undefined' ? document.activeElement : null);
  const uid = useId();
  const search = useProviders(query, providers);

  const groups = useMemo(
    () =>
      buildGroups({
        query,
        recents,
        nav: navItems(features, query),
        commands: commandItems(features, builtins, query),
        hits: search.query === query.trim() || search.pending ? search.hits : [],
      }),
    [query, recents, features, builtins, search],
  );
  const flat = useMemo(() => flatten(groups), [groups]);
  const current = flat[Math.min(active, flat.length - 1)] ?? null;

  useEffect(() => {
    input.current?.focus();
    const prev = restore.current;
    return () => {
      if (prev instanceof HTMLElement) prev.focus?.();
    };
  }, []);
  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    onActive?.(current?.href ?? null);
    const el = current ? document.getElementById(`${uid}-opt-${flat.indexOf(current)}`) : null;
    el?.scrollIntoView?.({ block: 'nearest' });
  }, [current, flat, uid, onActive]);

  const choose = (it: PaletteItem, newTab: boolean) => {
    if (it.href) {
      setRecents(pushRecent({ id: it.key, label: it.label, sublabel: it.sublabel, href: it.href, group: it.group }));
      onClose();
      if (newTab && openNewTab) openNewTab(it.href);
      else navigate(it.href);
    } else if (it.action) {
      onClose();
      it.action();
    }
  };

  const onKey = (e: KeyboardEvent) => {
    const n = flat.length;
    if (e.key === 'ArrowDown' || (e.key === 'n' && e.ctrlKey)) {
      e.preventDefault();
      if (n) setActive((a) => (Math.min(a, n - 1) + 1) % n);
    } else if (e.key === 'ArrowUp' || (e.key === 'p' && e.ctrlKey)) {
      e.preventDefault();
      if (n) setActive((a) => (Math.min(a, n - 1) - 1 + n) % n);
    } else if (e.key === 'Home' && e.ctrlKey) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === 'End' && e.ctrlKey) {
      e.preventDefault();
      setActive(Math.max(0, n - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (current) choose(current, e.ctrlKey || e.metaKey);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onClose();
    } else if (e.key === 'Tab') {
      e.preventDefault(); // focus stays in the palette
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      onClose();
    }
  };

  const q = query.trim();
  const showEmpty = q && !search.pending && flat.length === 0;
  return (
    <div className="v8-pal-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="v8-pal" role="dialog" aria-modal="true" aria-label="Palette de commandes et recherche" onKeyDown={onKey}>
        <div className="v8-pal-head">
          <svg className="v8-pal-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <path d="M10.5 10.5 14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          <input
            ref={input}
            className="v8-pal-input"
            role="combobox"
            aria-expanded="true"
            aria-controls={`${uid}-list`}
            aria-autocomplete="list"
            aria-activedescendant={current ? `${uid}-opt-${flat.indexOf(current)}` : undefined}
            aria-label="Rechercher une entité, une série, une commande"
            placeholder="Rechercher une entité, une série, une commande…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            spellCheck={false}
            autoComplete="off"
          />
          {search.pending && <span className="v8-pal-spin" role="status" aria-label="Recherche en cours" />}
          <button type="button" className="v8-pal-esc" onClick={onClose} aria-label="Fermer la palette">
            <KeyHint keys="Escape" />
          </button>
        </div>
        <div className="v8-pal-list" id={`${uid}-list`} role="listbox" aria-label="Résultats" ref={list}>
          {groups.map((g, gi) => (
            <div key={g.group} role="group" aria-labelledby={`${uid}-g-${gi}`} className="v8-pal-groupbox">
              <div className="v8-pal-group" id={`${uid}-g-${gi}`}>
                {g.group}
              </div>
              {g.items.map((it) => {
                const idx = flat.indexOf(it);
                const isActive = it === current;
                return (
                  <div
                    key={it.key}
                    id={`${uid}-opt-${idx}`}
                    role="option"
                    aria-selected={isActive}
                    className={`v8-pal-item${isActive ? ' v8-pal-active' : ''}`}
                    onMouseMove={() => idx !== active && setActive(idx)}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={(e) => choose(it, e.ctrlKey || e.metaKey)}
                  >
                    <span className="v8-pal-main">
                      <span className="v8-pal-label">
                        <Label text={it.label} query={q} />
                      </span>
                      {it.sublabel && <span className="v8-pal-sub">{it.sublabel}</span>}
                    </span>
                    {it.hint ? <KeyHint keys={it.hint} /> : isActive ? <span className="v8-pal-enter" aria-hidden="true">↵</span> : null}
                  </div>
                );
              })}
            </div>
          ))}
          {showEmpty && (
            <p className="v8-pal-empty" role="status">
              Aucun résultat pour « {q} »{search.failed ? ' (un service de recherche n’a pas répondu)' : ''}.
            </p>
          )}
          {!q && flat.length === 0 && <p className="v8-pal-empty">Tapez pour chercher une entité, un pays, un indicateur.</p>}
        </div>
        <div className="v8-pal-foot">
          <span>
            <KeyHint keys="ArrowUp ArrowDown" /> naviguer
          </span>
          <span>
            <KeyHint keys="Enter" /> ouvrir
          </span>
          <span>
            <KeyHint keys="Mod Enter" /> nouvel onglet
          </span>
          {footnote && <span className="v8-pal-note">{footnote}</span>}
        </div>
      </div>
    </div>
  );
}
