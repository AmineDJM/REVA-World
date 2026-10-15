import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { classifyV3Error } from '../api/common';
import { entityLabel, searchKg, type KgSearchHit } from '../api/kg';
import { kindLabel, kindStyle } from '../lib/kinds';

export interface PickedEntity {
  qid: string;
  label: string;
  kind?: string | null;
}

const DIRECT_ID = /^(Q[1-9]\d{0,11}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$/;
const ISO3 = /^[A-Z]{3}$/;

/**
 * Entity combobox over GET /v1/kg/search (debounced). A Wikidata id (Q…) — or, with `allowIso3`,
 * an ISO 3166 alpha-3 code — can be typed directly. Keyboard: ↑↓, Enter, Escape.
 */
export function EntityPicker({
  label,
  value,
  onChange,
  allowIso3 = false,
  placeholder = 'Rechercher une entité (ex. Brésil, Christophe Colomb, Q155)',
  testId,
}: {
  label: string;
  value: PickedEntity | null;
  onChange: (e: PickedEntity | null) => void;
  allowIso3?: boolean;
  placeholder?: string;
  testId?: string;
}) {
  const [text, setText] = useState(value?.label ?? '');
  const [hits, setHits] = useState<KgSearchHit[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [status, setStatus] = useState<'idle' | 'loading' | 'error'>('idle');
  const [note, setNote] = useState<string | null>(null);
  const listId = useId();
  const inputId = useId();
  const lastValue = useRef(value?.qid);
  const lastLabel = useRef(value?.label ?? '');

  useEffect(() => {
    const label = value?.label ?? '';
    // New entity, or a better label resolved for the same entity while the user is not typing.
    if (value?.qid !== lastValue.current || (label !== lastLabel.current && text === lastLabel.current)) {
      lastValue.current = value?.qid;
      setText(label);
    }
    lastLabel.current = label;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value?.qid, value?.label]);

  useEffect(() => {
    const q = text.trim();
    if (!open || q.length < 2 || (value && q === value.label)) {
      setHits([]);
      return;
    }
    const ctrl = new AbortController();
    const t = setTimeout(() => {
      setStatus('loading');
      searchKg(q, { limit: 12 }, ctrl.signal)
        .then((r) => {
          setHits(r.data.results);
          setStatus('idle');
          setNote(null);
          setActive(0);
        })
        .catch((e: unknown) => {
          if (ctrl.signal.aborted) return;
          const info = classifyV3Error(e);
          setStatus('error');
          setHits([]);
          setNote(
            info.kind === 'unavailable'
              ? 'Recherche pas encore disponible : saisissez un identifiant Wikidata (Q…)' + (allowIso3 ? ' ou un code ISO3.' : '.')
              : `${info.title}${info.detail ? ` : ${info.detail}` : ''}`,
          );
        });
    }, 220);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [text, open, value, allowIso3]);

  const direct = (() => {
    const q = text.trim();
    if (DIRECT_ID.test(q)) return { qid: q, label: q };
    if (allowIso3 && ISO3.test(q.toUpperCase()) && q.length === 3) return { qid: q.toUpperCase(), label: q.toUpperCase() };
    return null;
  })();
  const options: PickedEntity[] = [
    ...(direct ? [direct] : []),
    ...hits.map((h) => ({ qid: h.qid, label: entityLabel(h), kind: h.kind ?? null })),
  ];

  const choose = (e: PickedEntity | undefined) => {
    if (!e) return;
    lastValue.current = e.qid;
    lastLabel.current = e.label;
    setText(e.label);
    setOpen(false);
    onChange(e);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setOpen(true);
      setActive((a) => (options.length ? (a + 1) % options.length : 0));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => (options.length ? (a - 1 + options.length) % options.length : 0));
    } else if (e.key === 'Enter') {
      if (open && options.length) {
        e.preventDefault();
        choose(options[active]);
      }
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  };

  const optId = (i: number) => `${listId}-o${i}`;
  return (
    <div className="v3-picker" data-testid={testId}>
      <label htmlFor={inputId}>{label}</label>
      <div className="v3-picker-row">
        <input
          id={inputId}
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
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          onKeyDown={onKeyDown}
        />
        {value && (
          <button
            type="button"
            className="v3-picker-clear"
            onClick={() => {
              setText('');
              lastValue.current = undefined;
              onChange(null);
            }}
            aria-label={`Effacer ${label}`}
          >
            ×
          </button>
        )}
      </div>
      {open && options.length > 0 && (
        <ul id={listId} role="listbox" className="v3-picker-list" aria-label={`Suggestions pour ${label}`}>
          {options.map((o, i) => {
            const ks = kindStyle(o.kind);
            return (
              <li
                key={`${o.qid}-${i}`}
                id={optId(i)}
                role="option"
                aria-selected={i === active}
                className={i === active ? 'active' : undefined}
                onMouseDown={(e) => {
                  e.preventDefault();
                  choose(o);
                }}
                onMouseMove={() => setActive(i)}
              >
                <span className="v3-kind-dot" style={{ background: ks.color }} aria-hidden="true" />
                <span className="v3-picker-label">{o.label}</span>
                <span className="v3-picker-meta">
                  {o.kind ? kindLabel(o.kind) : direct && i === 0 ? 'identifiant saisi' : ''} · {o.qid}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {status === 'loading' && (
        <span className="v3-picker-status" role="status">
          recherche…
        </span>
      )}
      {note && (
        <span className="v3-picker-status field-hint" role="status">
          {note}
        </span>
      )}
    </div>
  );
}
