import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { V3Error, V3Loading } from '../../v3/components/States';
import { warningsFr } from '../../v3/api/common';
import { fetchDecisions, predictDecision, type DecisionDoc, type EstimateHop, type Hop, type Instrument, type Prediction, type VariableResult } from './api';
import { FanChart, IntervalBar } from './FanChart';
import {
  APPLICABILITY_FR,
  composeDecision,
  fmt,
  formFromDecision,
  gapFr,
  geoFr,
  isValidated,
  pathOf,
  unitFr,
  variableLabel,
  type DecisionForm,
} from './model';
import './decision.css';

type Catalog = { decisions: DecisionDoc[]; instruments: Instrument[]; label: string };

/**
 * "Décision": compose a decision (or pick a reference one), propagate it through attributed effect estimates and
 * REVA's simulators, and read: fan charts per variable, the causal path with its sources, the links that have no
 * number, and the validation status. Every result is labelled "scénario non validé — pas une probabilité" until
 * its model chain has passed the judge.
 */
export default function DecisionPage() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogError, setCatalogError] = useState<unknown>(null);
  const [form, setForm] = useState<DecisionForm | null>(null);
  const [refId, setRefId] = useState<string>('');
  const [draws, setDraws] = useState('2000');
  const [seed, setSeed] = useState('0');
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ prediction: Prediction; warnings: { code: string; message: string }[] } | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    const ctl = new AbortController();
    fetchDecisions(ctl.signal)
      .then((c) => {
        setCatalog(c);
        const first = c.decisions[0];
        if (first) {
          setRefId(first.id);
          setForm(formFromDecision(first));
        }
      })
      .catch((e: unknown) => {
        if (!(e instanceof DOMException && e.name === 'AbortError')) setCatalogError(e);
      });
    return () => ctl.abort();
  }, []);

  const instrument = useMemo(() => catalog?.instruments.find((i) => i.id === form?.instrument), [catalog, form?.instrument]);

  if (catalogError) return <V3Error error={catalogError} what="les décisions de référence" />;
  if (!catalog || !form) return <V3Loading what="des décisions" />;

  const set = (k: keyof DecisionForm) => (e: { target: { value: string } }) => {
    setRefId('');
    setForm({ ...form, [k]: e.target.value });
  };

  const pickReference = (id: string) => {
    const d = catalog.decisions.find((x) => x.id === id);
    setRefId(id);
    if (d) setForm(formFromDecision(d));
  };

  const pickInstrument = (id: string) => {
    const ins = catalog.instruments.find((i) => i.id === id);
    setRefId('');
    setForm({ ...form, instrument: id, unit: ins?.magnitude_units[0] ?? form.unit });
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const n = Number(draws);
    const s = Number(seed);
    const extra: string[] = [];
    if (!Number.isInteger(n) || n < 100 || n > 5000) extra.push('Le nombre de tirages va de 100 à 5 000.');
    if (!Number.isInteger(s) || s < 0) extra.push('La graine est un entier positif ou nul.');
    const ref = refId ? catalog.decisions.find((d) => d.id === refId) : undefined;
    const composed = ref ? { problems: [] as string[] } : composeDecision(form);
    const all = [...extra, ...composed.problems];
    setProblems(all);
    if (all.length) return;
    setBusy(true);
    setError(null);
    try {
      const body = ref ? { reference_id: ref.id, draws: n, seed: s } : { decision: composed.decision, draws: n, seed: s };
      setResult(await predictDecision(body));
    } catch (err) {
      setResult(null);
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="v5-decision" aria-labelledby="v5-decision-title">
      <header className="v5-head">
        <h1 id="v5-decision-title">Décision — que se passerait-il si… ?</h1>
        <p className="v5-label" data-testid="engine-label">
          {catalog.label}
        </p>
        <p className="muted">
          Une décision (qui, quoi, quand, combien) est propagée à travers des estimations d’effet attribuées (publication citée, calcul
          REVA ou identité) et les modèles de REVA. Les éventails ne reflètent que l’incertitude publiée par ces estimations. Un lien causal
          sans estimation chiffrée reste sans nombre. Recherche, pas un conseil.
        </p>
      </header>

      <form className="v5-form" onSubmit={submit} aria-label="Composer une décision">
        <label>
          Décision de référence
          <select value={refId} onChange={(e) => (e.target.value ? pickReference(e.target.value) : setRefId(''))}>
            <option value="">— décision composée —</option>
            {catalog.decisions.map((d) => (
              <option key={d.id} value={d.id}>
                {d.label_fr}
              </option>
            ))}
          </select>
        </label>
        <label>
          Intitulé
          <input value={form.label_fr} onChange={set('label_fr')} />
        </label>
        <label>
          Instrument
          <select value={form.instrument} onChange={(e) => pickInstrument(e.target.value)}>
            {catalog.instruments.map((i) => (
              <option key={i.id} value={i.id}>
                {i.label_fr}
              </option>
            ))}
          </select>
        </label>
        <fieldset>
          <legend>Qui et sur quoi</legend>
          <label>
            Acteur
            <input value={form.actor_label} onChange={set('actor_label')} />
          </label>
          <label>
            Pays de l’acteur (ISO3)
            <input value={form.actor_geo} onChange={set('actor_geo')} maxLength={3} size={4} />
          </label>
          <label>
            Cible
            <input value={form.target_label} onChange={set('target_label')} />
          </label>
          <label>
            Pays cible (ISO3)
            <input value={form.target_geo} onChange={set('target_geo')} maxLength={3} size={4} />
          </label>
        </fieldset>
        <fieldset>
          <legend>Combien et quand</legend>
          <label>
            Grandeur
            <input value={form.magnitude} onChange={set('magnitude')} inputMode="decimal" size={8} />
          </label>
          <label>
            Unité
            <select value={form.unit} onChange={set('unit')}>
              {(instrument?.magnitude_units ?? [form.unit]).map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
          </label>
          <label>
            Début (trimestre)
            <input value={form.start} onChange={set('start')} placeholder="2027Q1" size={7} />
          </label>
          <label>
            Durée (trimestres, vide = toute la période)
            <input value={form.duration} onChange={set('duration')} inputMode="numeric" size={4} />
          </label>
          <label>
            Horizon (trimestres)
            <input value={form.horizon} onChange={set('horizon')} inputMode="numeric" size={4} />
          </label>
          <label>
            Pays d’intérêt (ISO3)
            <input value={form.countries} onChange={set('countries')} placeholder="FRA, DEU" />
          </label>
        </fieldset>
        {instrument && (
          <ul className="v5-assumptions" aria-label="Hypothèses de l’instrument">
            {instrument.direct_changes.map((dc, i) => (
              <li key={i}>{dc.basis_fr}</li>
            ))}
          </ul>
        )}
        <fieldset>
          <legend>Monte-Carlo</legend>
          <label>
            Tirages
            <input value={draws} onChange={(e) => setDraws(e.target.value)} inputMode="numeric" size={6} />
          </label>
          <label>
            Graine
            <input value={seed} onChange={(e) => setSeed(e.target.value)} inputMode="numeric" size={6} />
          </label>
        </fieldset>
        {problems.length > 0 && (
          <ul className="v5-problems" role="alert">
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        )}
        <button type="submit" disabled={busy}>
          {busy ? 'Propagation…' : 'Propager la décision'}
        </button>
      </form>

      {error !== null && <V3Error error={error} what="la propagation de la décision" />}
      {result && <Result prediction={result.prediction} warnings={warningsFr(result.warnings)} />}
    </section>
  );
}

function Result({ prediction: p, warnings }: { prediction: Prediction; warnings: string[] }) {
  const quantified = p.variables;
  return (
    <div className="v5-result" data-testid="prediction">
      <section className={`v5-validation ${isValidated(p) ? 'is-validated' : 'is-unvalidated'}`} aria-label="Statut de validation">
        <h2>Statut : {p.label}</h2>
        {!isValidated(p) && (
          <>
            <p>
              Aucune probabilité n’est donnée : la chaîne de modèles n’a pas de rétro-test (millésimes) ni de dossier de calibration.{' '}
              {p.validation.reason ? <span className="muted">({p.validation.reason})</span> : null}
            </p>
            {p.validation.judge_requirements && (
              <ul>
                {p.validation.judge_requirements.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            )}
          </>
        )}
        <p className="muted">
          Signature de la chaîne : <code>{p.validation.chain_signature.slice(0, 16)}…</code> — {p.settings.draws} tirages, graine {p.settings.seed}.
          Couche d’estimations d’effet : {p.effect_layer.layer === 'present' ? 'présente' : `absente (${p.effect_layer.detail ?? ''})`}.
        </p>
        <ul className="v5-disclaimers">
          {[...p.disclaimers_fr, ...warnings.filter((w) => !p.disclaimers_fr.includes(w))].map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
      </section>

      <section aria-label="Changements directs">
        <h2>Changements directs (hypothèses de scénario)</h2>
        <ul>
          {p.direct_changes.map((dc, i) => (
            <li key={i}>
              <strong>
                {dc.variable.id} @ {geoFr(dc.geo)} : {dc.change} {dc.unit}
              </strong>{' '}
              — {dc.basis_fr}
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="Variables">
        <h2>Variables atteintes ({quantified.length})</h2>
        {quantified.length === 0 && (
          <p className="v5-empty" data-testid="no-variable">
            Aucune variable chiffrée : aucune estimation d’effet attribuée ni aucun modèle utilisable ne relie cette décision à une variable. REVA
            n’invente pas de nombre ; voir les liens non chiffrés et les lacunes ci-dessous.
          </p>
        )}
        <div className="v5-cards">
          {quantified.map((v) => (
            <VariableCard key={v.id} v={v} hops={p.hops} />
          ))}
        </div>
      </section>

      <section aria-label="Liens non chiffrés">
        <h2>Liens non chiffrés ({p.unquantified_links.length})</h2>
        <p className="muted">Des sources affirment ces liens, mais aucune estimation chiffrée et attribuée ne permet d’y mettre un nombre.</p>
        <ul data-testid="unquantified">
          {p.unquantified_links.map((u, i) => (
            <li key={i}>
              {u.wording_fr ?? u.note_fr}
              {u.grade ? <span className="v3-badge v3-badge-causal"> note {u.grade}</span> : null}
              {u.source?.source_key ? <span className="muted"> — {u.source.source_key}{u.source.tier ? ` (${u.source.tier})` : ''}</span> : null}
              <div className="muted">{u.note_fr}</div>
            </li>
          ))}
        </ul>
      </section>

      {(p.excluded_links.length > 0 || p.alternative_paths.length > 0) && (
        <section aria-label="Liens écartés">
          <h2>Estimations écartées et chemins non cumulés</h2>
          <ul>
            {p.excluded_links.map((x, i) => (
              <li key={`x${i}`}>
                <code>{x.estimate_key}</code>
                {x.geo ? ` (${x.geo})` : ''} : {x.detail}
              </li>
            ))}
            {p.alternative_paths.map((a, i) => (
              <li key={`a${i}`}>
                <code>{a.estimate_key}</code> → {a.to} : {a.reason_fr}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-label="Lacunes">
        <h2>Lacunes ({p.gaps.length})</h2>
        <ul data-testid="gaps">
          {p.gaps.map((g, i) => (
            <li key={i}>{gapFr(g as { code: string; message?: unknown; message_fr?: unknown })}</li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function VariableCard({ v, hops }: { v: VariableResult; hops: Hop[] }) {
  const label = `${variableLabel(v)} — ${geoFr(v.geo)}`;
  const unit = unitFr(v.unit);
  const path = pathOf(v, hops);
  return (
    <article className="v5-card" data-testid="variable-card">
      <h3>{label}</h3>
      <p className="muted">
        Écart par rapport à la référence, en {unit}
        {v.kind === 'plugin_output' ? ` — sortie du modèle ${v.model ?? ''}` : ''}
      </p>
      {!v.uncertainty_complete && (
        <p className="v5-warn" role="note">
          Distribution incomplète : {v.point_estimates.length} estimation(s) sans incertitude publiée sur ce chemin (valeurs ponctuelles de scénario).
        </p>
      )}
      {v.frequency === 'undated' && v.undated ? (
        <>
          <p>
            {v.undated.kind_label_fr ?? v.undated.kind ?? 'non daté'} : médiane <strong>{fmt(v.undated.p50)}</strong> {unit} (5–95 % : {fmt(v.undated.p05)} à{' '}
            {fmt(v.undated.p95)})
          </p>
          <IntervalBar q={v.undated} unit={unit} label={label} kind={v.undated.kind_label_fr ?? 'non daté'} />
        </>
      ) : (
        <FanChart periods={v.periods} unit={unit} label={label} />
      )}
      {v.baseline && (
        <p className="muted">
          Référence : {v.baseline.value} {v.baseline.unit} ({v.baseline.year}, {v.baseline.source}) — {v.baseline.label_fr}
        </p>
      )}
      <details>
        <summary>Chemin causal et sources ({path.length} étapes)</summary>
        <ol className="v5-path">
          {path.map((h) => (
            <li key={h.id}>
              <HopLine h={h} />
            </li>
          ))}
        </ol>
        {v.notes.length > 0 && (
          <ul className="muted">
            {v.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        )}
      </details>
    </article>
  );
}

function HopLine({ h }: { h: Hop }) {
  if (h.kind === 'decision') return <span>Décision → {h.to} — {h.direct_change.basis_fr}</span>;
  if (h.kind === 'plugin')
    return (
      <span>
        Modèle <strong>{h.model}</strong> ({h.label_fr}) — {h.assumption_fr}
      </span>
    );
  return <EstimateLine h={h} />;
}

function EstimateLine({ h }: { h: EstimateHop }) {
  const s = h.source;
  return (
    <span data-testid="estimate-hop">
      {s.wording_fr ?? `${h.from} → ${h.to}`}{' '}
      <span className="muted">
        [<code>{h.estimate_key}</code>
        {s.locator ? `, ${s.locator}` : ''}] coefficient {fmt(h.coefficient.point)}
        {h.coefficient.sd != null ? ` ± ${fmt(h.coefficient.sd)} (σ, ${h.coefficient.uncertainty_basis})` : ' (incertitude non publiée)'} —{' '}
        {APPLICABILITY_FR[h.applicability.status] ?? h.applicability.status}
      </span>
      {s.url ? (
        <>
          {' '}
          <a href={s.url} rel="noreferrer noopener" target="_blank">
            source
          </a>
        </>
      ) : null}
      {h.alternatives.length > 0 && <span className="muted"> — {h.alternatives.length} autre(s) estimation(s) du même lien, non additionnée(s)</span>}
    </span>
  );
}
