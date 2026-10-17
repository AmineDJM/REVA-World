import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { classifyV3Error } from '../../../v3/api/common';
import { getCatalog, getMeasure, getTable, type CatalogHit, type CatalogResponse, type MeasureResponse, type Selector, type TableResponse } from './api';
import { selectorOfSeries } from './url';

const GROUP_LABEL: Record<string, string> = { concept: 'Indicateurs du catalogue', measure: 'Codes de mesure', table: 'Tables' };

export interface PickerProps {
  /** Current indicator label (button text). */
  current: string | null;
  sublabel?: string | null;
  onPick: (s: Selector, hit?: CatalogHit) => void;
  /** Keyboard shortcut focusing the picker ("/" for X). */
  hotkey?: string;
  placeholder?: string;
  testId?: string;
}

type View = { kind: 'search' } | { kind: 'table'; table: string };

/**
 * Indicator search over the whole atlas: catalog concepts (labels fr/en, theme, unit, sources), raw measure
 * codes and tables. Keyboard: ↑ ↓ to move, Entrée to choose, Échap to close.
 */
export function IndicatorPicker({ current, sublabel, onPick, hotkey, placeholder = 'Chercher un indicateur (chômage, PIB, CO2, NY.GDP…)', testId = 'indicator-picker' }: PickerProps) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [theme, setTheme] = useState<string | null>(null);
  const [view, setView] = useState<View>({ kind: 'search' });
  const [res, setRes] = useState<CatalogResponse | null>(null);
  const [tableRes, setTableRes] = useState<TableResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const listId = useId();

  useEffect(() => {
    if (!hotkey) return;
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
      if (e.key === hotkey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        setOpen(true);
      }
    };
    window.addEventListener('keydown', on);
    return () => window.removeEventListener('keydown', on);
  }, [hotkey]);

  useEffect(() => {
    if (open) setTimeout(() => input.current?.focus(), 0);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [open]);

  useEffect(() => {
    if (!open || view.kind !== 'search') return;
    const ctrl = new AbortController();
    const t = setTimeout(() => {
      getCatalog(q, theme, ctrl.signal)
        .then((r) => {
          setRes(r);
          setErr(null);
          setActive(0);
        })
        .catch((e: unknown) => {
          if (!ctrl.signal.aborted) setErr(classifyV3Error(e).title);
        });
    }, 110);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [q, theme, open, view.kind]);

  useEffect(() => {
    if (view.kind !== 'table') return;
    const ctrl = new AbortController();
    const t = setTimeout(() => {
      getTable(view.table, q, ctrl.signal)
        .then((r) => {
          setTableRes(r);
          setActive(0);
        })
        .catch((e: unknown) => {
          if (!ctrl.signal.aborted) setErr(classifyV3Error(e).title);
        });
    }, 110);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [view, q]);

  const items: { key: string; group: string; label: string; sub: string; dim?: boolean; choose: () => void }[] = useMemo(() => {
    if (view.kind === 'table') {
      return (tableRes?.measures ?? []).map((m) => ({
        key: m.measure,
        group: `Mesures de ${view.table}`,
        label: m.label ?? m.measure,
        sub: `${m.measure}${m.variants > 1 ? ` · ${m.variants} variantes` : ''}`,
        choose: () => {
          onPick({ table: view.table, measure: m.measure, filters: {} });
          setOpen(false);
        },
      }));
    }
    return (res?.hits ?? []).map((h) => ({
      key: `${h.kind}:${h.id}`,
      group: GROUP_LABEL[h.kind] ?? h.kind,
      label: h.label,
      sub: h.sublabel ?? '',
      dim: !h.in_snapshot,
      choose: () => {
        if (h.kind === 'table' && h.table) {
          setView({ kind: 'table', table: h.table });
          setQ('');
          return;
        }
        if (!h.table || h.measure === null || h.measure === undefined) return;
        onPick(selectorOfSeries(h.table, h.measure, h.filters), h);
        setOpen(false);
      },
    }));
  }, [res, tableRes, view, onPick]);

  const themes = res ? Object.entries(res.themes as Record<string, { fr?: string; en?: string }>) : [];

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => Math.min(items.length - 1, a + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      items[active]?.choose();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      if (view.kind === 'table') setView({ kind: 'search' });
      else setOpen(false);
    } else if (e.key === 'Backspace' && q === '' && view.kind === 'table') setView({ kind: 'search' });
  };

  let lastGroup = '';
  return (
    <div className="v8m-picker" ref={root} data-testid={testId}>
      <button type="button" className="v8m-picker-btn" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="v8m-picker-label">{current ?? 'Choisir un indicateur'}</span>
        {sublabel && <span className="v8m-picker-sub">{sublabel}</span>}
        {hotkey && <kbd className="v8m-kbd">{hotkey}</kbd>}
      </button>
      {open && (
        <div className="v8m-picker-pop" role="dialog" aria-label="Choisir un indicateur">
          <div className="v8m-picker-search">
            {view.kind === 'table' && (
              <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={() => setView({ kind: 'search' })}>
                ← {view.table}
              </button>
            )}
            <input
              ref={input}
              className="v8-input"
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={onKey}
              placeholder={view.kind === 'table' ? 'Filtrer les mesures de la table' : placeholder}
              role="combobox"
              aria-expanded="true"
              aria-controls={listId}
              aria-activedescendant={items[active] ? `${listId}-${active}` : undefined}
              aria-label="Recherche d’indicateur"
            />
          </div>
          {view.kind === 'search' && themes.length > 0 && (
            <div className="v8m-themes" role="group" aria-label="Thèmes">
              {themes.map(([id, t]) => (
                <button key={id} type="button" className={`v8m-chip${theme === id ? ' is-on' : ''}`} aria-pressed={theme === id} onClick={() => setTheme(theme === id ? null : id)}>
                  {t.fr ?? id}
                </button>
              ))}
            </div>
          )}
          {err && <p className="v8m-picker-err">{err}</p>}
          <ul className="v8m-picker-list" role="listbox" id={listId}>
            {items.length === 0 && <li className="v8m-picker-empty">{res || tableRes ? 'Aucun indicateur ne correspond.' : 'Recherche…'}</li>}
            {items.map((it, i) => {
              const head = it.group !== lastGroup ? it.group : null;
              lastGroup = it.group;
              return (
                <li key={it.key} role="presentation">
                  {head && <div className="v8m-picker-group">{head}</div>}
                  <div
                    id={`${listId}-${i}`}
                    role="option"
                    aria-selected={i === active}
                    className={`v8m-picker-item${i === active ? ' is-active' : ''}${it.dim ? ' is-dim' : ''}`}
                    onMouseEnter={() => setActive(i)}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      it.choose();
                    }}
                  >
                    <span className="v8m-picker-item-label">{it.label}</span>
                    <span className="v8m-picker-item-sub">{it.sub}</span>
                  </div>
                </li>
              );
            })}
          </ul>
          {res && view.kind === 'search' && (
            <p className="v8m-picker-foot">
              {res.concepts_total} indicateurs du catalogue correspondent · définitions déclarées par les fournisseurs, non relues
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** Filter facets of a measure (sex, age, unit…): one select per key; changing one keeps the others. */
export function FilterBar({ sel, onChange }: { sel: Selector | null; onChange: (s: Selector) => void }) {
  const [m, setM] = useState<MeasureResponse | null>(null);
  const table = sel?.table ?? null;
  const measure = sel?.measure ?? null;
  const filters = useMemo(() => sel?.filters ?? {}, [sel]);
  const fkey = JSON.stringify(filters);
  useEffect(() => {
    if (!table || measure === null) {
      setM(null);
      return;
    }
    let live = true;
    getMeasure(table, measure, filters)
      .then((r) => live && setM(r))
      .catch(() => live && setM(null));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [table, measure, fkey]);
  if (!m || m.facets.length === 0 || !table || measure === null) return null;
  const resolved = exactFilters(m.resolved.indicator);
  return (
    <div className="v8m-filters" role="group" aria-label="Filtres de la mesure" data-testid="filter-bar">
      {m.facets.map((f) => (
        <label key={f.key} className="v8m-filter">
          <span>{f.key}</span>
          <select
            className="v8-input"
            value={resolved[f.key] ?? ''}
            onChange={(e) => {
              const want = { ...filters, [f.key]: e.target.value };
              // keep the other explicit filters when that combination exists, else only the changed one
              getMeasure(table, measure, want)
                .catch(() => getMeasure(table, measure, { [f.key]: e.target.value }))
                .then((r) => onChange({ table, measure, filters: exactFilters(r.resolved.indicator) }))
                .catch(() => onChange({ table, measure, filters: { [f.key]: e.target.value } }));
            }}
          >
            {f.values.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
      ))}
    </div>
  );
}

/** key=value filters of an atlas indicator ("UNE|sex=SEX_T|age=Y15-24" → { sex: 'SEX_T', age: 'Y15-24' }). */
export function exactFilters(indicator: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const tok of indicator.split('|')) {
    const i = tok.indexOf('=');
    if (i > 0) out[tok.slice(0, i)] = tok.slice(i + 1);
  }
  return out;
}
