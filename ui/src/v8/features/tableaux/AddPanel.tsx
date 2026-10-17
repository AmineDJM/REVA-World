/**
 * Add an item to a board: type to search (V8 universal search, /v1/v8/search) or paste a V8 link / forecast id.
 * Arrow keys move in the results, Enter adds, Escape closes. Indicators ask for a country (ISO3).
 */
import { useEffect, useId, useRef, useState } from 'react';
import { classifyV3Error } from '../../../v3/api/common';
import { searchV8, type SearchHit } from './api';
import { itemTitle, KIND_LABEL_FR, type PinItem } from './model';
import { hitToPin, urlToPin } from './rules';

export function AddPanel({ onAdd, onClose }: { onAdd: (p: PinItem) => boolean; onClose: () => void }) {
  const [q, setQ] = useState('');
  const [geo, setGeo] = useState('FRA');
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [state, setState] = useState<{ s: 'idle' | 'loading' | 'ok' | 'error'; err?: unknown }>({ s: 'idle' });
  const [active, setActive] = useState(0);
  const [msg, setMsg] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const listId = useId();

  useEffect(() => input.current?.focus(), []);

  const direct = urlToPin(q);
  useEffect(() => {
    const t = q.trim();
    if (!t || direct) {
      setHits([]);
      setState({ s: 'idle' });
      return;
    }
    const ctrl = new AbortController();
    const h = setTimeout(() => {
      setState({ s: 'loading' });
      searchV8(t, ctrl.signal)
        .then((r) => {
          setHits(r);
          setActive(0);
          setState({ s: 'ok' });
        })
        .catch((err: unknown) => {
          if (ctrl.signal.aborted) return;
          setHits([]);
          setState({ s: 'error', err });
        });
    }, 160);
    return () => {
      clearTimeout(h);
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, !!direct]);

  const options: { key: string; pin: PinItem | null; hit?: SearchHit; label: string; sub: string }[] = direct
    ? [{ key: 'direct', pin: direct, label: itemTitle(direct), sub: `${KIND_LABEL_FR[direct.kind]} · depuis le lien collé` }]
    : hits.map((h) => ({ key: `${h.kind}:${h.id}`, pin: hitToPin(h, geo), hit: h, label: h.label, sub: [h.group, h.sublabel].filter(Boolean).join(' · ') }));

  const choose = (i: number) => {
    const o = options[i];
    if (!o) return;
    if (!o.pin) {
      setMsg('Indicateur : indiquez un pays (code ISO3, ex. FRA) pour épingler sa série.');
      return;
    }
    const added = onAdd(o.pin);
    setMsg(added ? `Ajouté : ${itemTitle(o.pin)}` : `Déjà sur ce tableau : ${itemTitle(o.pin)}`);
    if (added) setQ('');
    input.current?.focus();
  };

  const showGeo = hits.some((h) => h.kind === 'indicator');

  return (
    <div className="v8b-panel v8b-add" role="search" aria-label="Ajouter au tableau">
      <div className="v8b-row">
        <label htmlFor="v8b-add-q" className="v8b-label">
          Ajouter
        </label>
        <input
          id="v8b-add-q"
          ref={input}
          className="v8b-input v8b-grow"
          role="combobox"
          aria-expanded={options.length > 0}
          aria-controls={listId}
          aria-activedescendant={options[active] ? `${listId}-${active}` : undefined}
          aria-autocomplete="list"
          placeholder="Pays, entité, indicateur, question fq_…, décision — ou collez un lien /v8/…"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setMsg('');
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') {
              e.preventDefault();
              setActive((a) => Math.min(options.length - 1, a + 1));
            } else if (e.key === 'ArrowUp') {
              e.preventDefault();
              setActive((a) => Math.max(0, a - 1));
            } else if (e.key === 'Enter') {
              e.preventDefault();
              choose(active);
            } else if (e.key === 'Escape') {
              e.preventDefault();
              e.stopPropagation();
              onClose();
            }
          }}
        />
        {showGeo && (
          <>
            <label htmlFor="v8b-add-geo" className="v8b-label">
              Pays
            </label>
            <input
              id="v8b-add-geo"
              className="v8b-input v8b-geo"
              value={geo}
              maxLength={3}
              onChange={(e) => setGeo(e.target.value.toUpperCase())}
              title="Code ISO3 du pays pour les indicateurs (série)"
            />
          </>
        )}
        <button type="button" className="v8b-btn" onClick={onClose}>
          Fermer <kbd>Échap</kbd>
        </button>
      </div>
      {state.s === 'error' && (
        <p className={classifyV3Error(state.err).kind === 'unavailable' ? 'v8b-soft' : 'v8b-err'} role="status">
          Recherche universelle : {classifyV3Error(state.err).title.toLowerCase()}. Collez plutôt un lien V8 (fiche, décision, monde avec concept et pays) ou un identifiant
          de question <code>fq_…</code>.
        </p>
      )}
      {state.s === 'ok' && hits.length === 0 && <p className="v8b-soft">Aucun résultat pour « {q} ».</p>}
      {options.length > 0 && (
        <ul id={listId} role="listbox" className="v8b-hits" aria-label="Résultats">
          {options.map((o, i) => (
            <li
              id={`${listId}-${i}`}
              key={o.key}
              role="option"
              aria-selected={i === active}
              className={`v8b-hit${i === active ? ' is-active' : ''}${o.pin ? '' : ' is-incomplete'}`}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault();
                choose(i);
              }}
            >
              <span className="v8b-hit-label">{o.label}</span>
              <span className="v8b-hit-sub">
                {o.pin ? KIND_LABEL_FR[o.pin.kind] : 'Série — pays requis'}
                {o.sub ? ` · ${o.sub}` : ''}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="v8b-meta" aria-live="polite">
        {msg}
      </p>
    </div>
  );
}
