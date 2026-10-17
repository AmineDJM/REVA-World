import { useRef, type KeyboardEvent, type ReactNode } from 'react';
import { useUrlParam } from './useUrlParam';

export interface TabItem {
  id: string;
  label: ReactNode;
  count?: number | null;
  disabled?: boolean;
}

/**
 * Tab list with roving focus (←/→/Home/End). Controlled (`value` + `onChange`) — or use <UrlTabs param="tab">
 * to keep the selected tab in the URL. Render the panel yourself with id `${idBase}-panel-${tab}`.
 */
export function Tabs({ tabs, value, onChange, label, idBase = 'v8-tabs', size = 'md' }: { tabs: TabItem[]; value: string; onChange: (id: string) => void; label: string; idBase?: string; size?: 'sm' | 'md' }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const enabled = tabs.filter((t) => !t.disabled);
  const onKey = (e: KeyboardEvent) => {
    const i = enabled.findIndex((t) => t.id === value);
    let j = -1;
    if (e.key === 'ArrowRight') j = (i + 1) % enabled.length;
    else if (e.key === 'ArrowLeft') j = (i - 1 + enabled.length) % enabled.length;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = enabled.length - 1;
    if (j < 0) return;
    e.preventDefault();
    const t = enabled[j]!;
    onChange(t.id);
    refs.current[tabs.indexOf(t)]?.focus();
  };
  return (
    <div className={`v8-tabs v8-tabs-${size}`} role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t, i) => (
        <button
          key={t.id}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          role="tab"
          id={`${idBase}-tab-${t.id}`}
          aria-selected={t.id === value}
          aria-controls={`${idBase}-panel-${t.id}`}
          tabIndex={t.id === value ? 0 : -1}
          disabled={t.disabled}
          className="v8-tab"
          onClick={() => onChange(t.id)}
        >
          {t.label}
          {t.count !== undefined && t.count !== null && <span className="v8-tab-count v8-num">{t.count.toLocaleString('fr-FR')}</span>}
        </button>
      ))}
    </div>
  );
}

/** Tabs whose selection lives in ?<param>= (default tab = first, not written to the URL). */
export function UrlTabs({ tabs, param = 'tab', label, idBase }: { tabs: TabItem[]; param?: string; label: string; idBase?: string }) {
  const [value, setValue] = useUrlParam(param, tabs[0]?.id ?? '');
  return <Tabs tabs={tabs} value={value} onChange={(v) => setValue(v, { replace: false })} label={label} idBase={idBase} />;
}

/** The panel matching a tab (role=tabpanel, labelled by its tab). */
export function TabPanel({ id, idBase = 'v8-tabs', children }: { id: string; idBase?: string; children: ReactNode }) {
  return (
    <div role="tabpanel" id={`${idBase}-panel-${id}`} aria-labelledby={`${idBase}-tab-${id}`} className="v8-tabpanel">
      {children}
    </div>
  );
}
