import { forwardRef, useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { kindLabel, kindStyle } from '../../../v3/lib/kinds';
import { searchNodes, searchUniversal, type NodeHit } from './api';

export interface PickedNode {
  id: string;
  label: string;
}

const DIRECT = /^(Q[1-9]\d{0,11}|[A-Z]{3}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,160}(@[A-Z]{3})?)$/;

function mergeHits(a: NodeHit[], b: NodeHit[]): NodeHit[] {
  const seen = new Set<string>();
  const out: NodeHit[] = [];
  for (const h of [...a, ...b]) {
    if (seen.has(h.node.id)) continue;
    seen.add(h.node.id);
    out.push(h);
  }
  return out;
}

/**
 * Node combobox for Liens: path-index search (KG entities + effect variables / atlas indicators / event types,
 * with their number of indexed links) merged with the universal search (/v1/v8/search) when it answers.
 * A typed id (Q…, ISO3, prefix:id) is offered as is. Keyboard: ↑↓, Entrée, Échap.
 */
export const NodePicker = forwardRef<HTMLInputElement, { label: string; value: PickedNode | null; onChange: (v: PickedNode | null) => void; placeholder: string; testId?: string }>(
  function NodePicker({ label, value, onChange, placeholder, testId }, ref) {
    const [text, setText] = useState(value?.label ?? '');
    const [hits, setHits] = useState<NodeHit[]>([]);
    const [open, setOpen] = useState(false);
    const [active, setActive] = useState(0);
    const [loading, setLoading] = useState(false);
    const listId = useId();
    const inputId = useId();
    const shown = useRef(value?.label ?? '');

    useEffect(() => {
      if ((value?.label ?? '') !== shown.current) {
        shown.current = value?.label ?? '';
        setText(shown.current);
      }
    }, [value?.label]);

    useEffect(() => {
      const q = text.trim();
      if (!open || q.length < 2 || (value && q === value.label)) {
        setHits([]);
        return;
      }
      const ctrl = new AbortController();
      const t = setTimeout(() => {
        setLoading(true);
        let local: NodeHit[] = [];
        let uni: NodeHit[] = [];
        const done = () => !ctrl.signal.aborted && setHits(mergeHits(local, uni));
        const a = searchNodes(q, ctrl.signal)
          .then((h) => {
            local = h;
            done();
          })
          .catch(() => undefined);
        const b = searchUniversal(q, ctrl.signal).then((h) => {
          uni = h;
          done();
        });
        void Promise.all([a, b]).finally(() => !ctrl.signal.aborted && setLoading(false));
      }, 160);
      return () => {
        clearTimeout(t);
        ctrl.abort();
      };
    }, [text, open, value]);

    const q = text.trim();
    const direct: NodeHit | null =
      DIRECT.test(q) && !hits.some((h) => h.node.id === q)
        ? { node: { id: q, label: q, label_en: null, label_loaded: false, kind: null, kind_label: 'identifiant saisi', virtual: false, scope_iso3: null, fiche: null, degree: null }, match: 'id', indexed: true }
        : null;
    const options = direct ? [direct, ...hits] : hits;

    const choose = (h: NodeHit | undefined) => {
      if (!h) return;
      shown.current = h.node.label;
      setText(h.node.label);
      setOpen(false);
      onChange({ id: h.node.id, label: h.node.label });
    };

    const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setOpen(true);
        setActive((a) => (options.length ? (a + 1) % options.length : 0));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActive((a) => (options.length ? (a - 1 + options.length) % options.length : 0));
      } else if (e.key === 'Enter' && open && options.length) {
        e.preventDefault();
        choose(options[Math.min(active, options.length - 1)]);
      } else if (e.key === 'Escape') {
        setOpen(false);
        (e.target as HTMLInputElement).blur();
      }
    };

    const optId = (i: number) => `${listId}-o${i}`;
    return (
      <div className="lien-picker" data-testid={testId}>
        <label htmlFor={inputId} className="lien-picker-label">
          {label}
        </label>
        <div className="lien-picker-row">
          <input
            ref={ref}
            id={inputId}
            className="v8-input lien-picker-input"
            type="text"
            role="combobox"
            aria-expanded={open && options.length > 0}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={open && options[active] ? optId(active) : undefined}
            value={text}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => {
              setText(e.target.value);
              setOpen(true);
              setActive(0);
            }}
            onFocus={(e) => {
              setOpen(true);
              e.target.select();
            }}
            onBlur={() => setTimeout(() => setOpen(false), 150)}
            onKeyDown={onKeyDown}
          />
          {loading && <span className="lien-picker-spin" role="status" aria-label="recherche…" />}
        </div>
        {open && options.length > 0 && (
          <ul id={listId} role="listbox" className="lien-picker-list" aria-label={`Suggestions : ${label}`}>
            {options.map((h, i) => {
              const ks = kindStyle(h.node.kind);
              const meta = h.node.kind_label ?? (h.node.kind ? kindLabel(h.node.kind) : null);
              return (
                <li
                  key={`${h.node.id}-${i}`}
                  id={optId(i)}
                  role="option"
                  aria-selected={i === active}
                  className={`lien-opt${i === active ? ' is-active' : ''}${h.indexed ? '' : ' is-unindexed'}`}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    choose(h);
                  }}
                  onMouseMove={() => setActive(i)}
                >
                  <span className={`lien-dot${h.node.virtual ? ' is-virtual' : ''}`} style={{ background: ks.color }} aria-hidden="true" />
                  <span className="lien-opt-label">{h.node.label}</span>
                  <span className="lien-opt-meta">
                    {meta ? `${meta} · ` : ''}
                    <span className="v8-mono">{h.node.id}</span>
                    {h.node.degree !== null ? ` · ${h.node.degree.toLocaleString('fr-FR')} liens` : ''}
                    {!h.indexed ? ' · aucun lien indexé' : ''}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    );
  },
);
