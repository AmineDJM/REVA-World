import { useEffect, useMemo, useState, type FormEvent } from 'react';
import type { DecisionDoc } from '../../../v5/decision/api';
import type { Templates } from './api';
import { fieldsFromDecision, overridesOf, unitFr, validateFields, type ScenarioFields } from './state';

/**
 * Composer: the five reference decisions as one-click templates, then free composition (actor, instrument,
 * magnitude, date, scope) validated against the server's decision schema before any request.
 */
export function Composer({
  templates,
  current,
  base,
  onRun,
}: {
  templates: Templates;
  /** The decision currently shown (reference, variant or composed), or null. */
  current: DecisionDoc | null;
  /** The reference the current scenario derives from, if any. */
  base: DecisionDoc | null;
  onRun: (fields: ScenarioFields) => void;
}) {
  const start = useMemo<ScenarioFields>(() => (current ? fieldsFromDecision(current) : fieldsFromDecision(templates.references[0]!.decision)), [current, templates]);
  const [draft, setDraft] = useState<ScenarioFields>(start);
  const [from, setFrom] = useState<DecisionDoc | null>(base ?? (current && templates.references.some((r) => r.id === current.id) ? current : null));
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    setDraft(start);
    setFrom(base ?? (current && templates.references.some((r) => r.id === current.id) ? current : null));
    setTouched(false);
  }, [start, base, current, templates]);

  const ins = templates.instruments.find((i) => i.id === draft.i);
  const problems = validateFields(draft, ins, templates.schema);
  const set = (k: keyof ScenarioFields) => (e: { target: { value: string } }) => {
    setTouched(true);
    setDraft((d) => ({ ...d, [k]: e.target.value }));
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (problems.length) return;
    const fields: ScenarioFields = { ...draft };
    if (fields.l === (from?.label_fr ?? '')) delete fields.l; // label follows the variant unless edited
    onRun(from ? { ref: from.id, ...overridesOf(fields, from) } : fields);
  };

  return (
    <div className="v8d-composer">
      <div className="v8d-templates" role="list" aria-label="Décisions de référence">
        {templates.references.map((r, i) => (
          <button
            key={r.id}
            type="button"
            role="listitem"
            className={`v8d-template${current?.id === r.id ? ' is-on' : ''}`}
            onClick={() => onRun({ ref: r.id })}
            title={r.summary_fr}
          >
            <span className="v8d-template-k" aria-hidden="true">
              {i + 1}
            </span>
            <span className="v8d-template-t">{r.decision.label_fr}</span>
            <span className="v8d-template-s">{r.instrument_label_fr}</span>
          </button>
        ))}
      </div>
      <details className="v8d-compose" open={touched || undefined}>
        <summary>
          Composer ou modifier la décision
          {from ? <span className="v8-muted">{from.id === current?.id ? ' — point de départ : la décision de référence affichée' : ` — variante de « ${from.label_fr} »`}</span> : null}
        </summary>
        <form onSubmit={submit} noValidate aria-label="Composer une décision">
          <div className="v8d-form-grid">
            <label>
              Instrument
              <select
                className="v8-input"
                value={draft.i ?? ''}
                onChange={(e) => {
                  const next = templates.instruments.find((x) => x.id === e.target.value);
                  setTouched(true);
                  setFrom(null);
                  setDraft((d) => ({ ...d, i: e.target.value, u: next?.magnitude_units[0] ?? '', m: next?.needs.occurrence ? '1' : d.m }));
                }}
              >
                {templates.instruments.map((x) => (
                  <option key={x.id} value={x.id}>
                    {x.label_fr}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Grandeur
              <input className="v8-input v8-num" inputMode="decimal" value={draft.m ?? ''} onChange={set('m')} aria-describedby="v8d-mag-help" />
            </label>
            <label>
              Unité
              <select className="v8-input" value={draft.u ?? ''} onChange={set('u')}>
                {(ins?.magnitude_units ?? []).map((u) => (
                  <option key={u} value={u}>
                    {unitFr(ins, u)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Début (trimestre)
              <input className="v8-input v8-num" placeholder="2027Q1" value={draft.s ?? ''} onChange={set('s')} />
            </label>
            <label>
              Durée (trimestres, vide = maintenue)
              <input className="v8-input v8-num" inputMode="numeric" value={draft.d ?? ''} onChange={set('d')} />
            </label>
            <label>
              Horizon (trimestres)
              <input className="v8-input v8-num" inputMode="numeric" value={draft.h ?? ''} onChange={set('h')} />
            </label>
            <label className="v8d-wide">
              Pays d’intérêt (ISO3)
              <input className="v8-input" placeholder="DEU, FRA" value={draft.c ?? ''} onChange={set('c')} />
            </label>
            <label>
              Acteur
              <input className="v8-input" value={draft.al ?? ''} onChange={set('al')} />
            </label>
            <label>
              Pays de l’acteur{ins?.needs.actor_geo ? ' *' : ''}
              <input className="v8-input" placeholder="IND" value={draft.ag ?? ''} onChange={set('ag')} />
            </label>
            <label>
              Cible
              <input className="v8-input" value={draft.tl ?? ''} onChange={set('tl')} />
            </label>
            <label>
              Pays visé{ins?.needs.target_geo ? ' *' : ''}
              <input className="v8-input" placeholder="RUS" value={draft.tg ?? ''} onChange={set('tg')} />
            </label>
            <label className="v8d-wide">
              Intitulé (facultatif)
              <input className="v8-input" value={draft.l ?? ''} onChange={set('l')} />
            </label>
          </div>
          <p id="v8d-mag-help" className="v8d-help">
            {ins?.direct_changes[0]?.basis_fr}
            {ins?.needs.occurrence ? ' Grandeur 1 = mesure complète (seule propagée).' : ''}
            {ins?.needs.level ? ' Niveau cible (p. ex. un âge), pas une variation.' : ''}
          </p>
          {touched && problems.length > 0 && (
            <ul className="v8d-problems" role="alert">
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}
          <div className="v8-row">
            <button type="submit" className="v8-btn v8-btn-primary" disabled={problems.length > 0}>
              Propager ⏎
            </button>
            <span className="v8-muted v8d-small">Chaque résultat reste un scénario conditionnel, pas une prévision.</span>
          </div>
        </form>
      </details>
    </div>
  );
}
