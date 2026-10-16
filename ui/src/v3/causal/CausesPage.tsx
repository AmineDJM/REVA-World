import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { classifyV3Error } from '../api/common';
import { EntityPicker, type PickedEntity } from '../components/EntityPicker';
import { V3Error, V3Loading } from '../components/States';
import {
  getCausalCandidates,
  getCausalClaims,
  getCausalEvent,
  getCausalWhy,
  type CausalCandidate,
  type CausalClaim,
  type CausalEvent,
  type ClaimsResult,
  type WhyResult,
} from './api';
import { CausalTimeline } from './CausalTimeline';
import { ChainFinder } from './ChainFinder';
import { buildGraph, dissentPairs, groupWhy } from './model';
import { CANDIDATE_LABEL, CandidateList, CausalLegend, ClaimDetail, ClaimLine, DissentPanel, EventCard, WhyPanel } from './parts';
import './causal.css';

const CausalGraph = lazy(() => import('./CausalGraph'));

type Load<T> = { status: 'loading' } | { status: 'ok'; data: T } | { status: 'error'; error: unknown };

function useLoad<T>(key: string | null, fn: (signal: AbortSignal) => Promise<T>): Load<T> | null {
  const [state, setState] = useState<Load<T> | null>(null);
  useEffect(() => {
    if (!key) {
      setState(null);
      return;
    }
    const ctrl = new AbortController();
    setState({ status: 'loading' });
    fn(ctrl.signal)
      .then((data) => !ctrl.signal.aborted && setState({ status: 'ok', data }))
      .catch((error: unknown) => {
        if (!ctrl.signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) setState({ status: 'error', error });
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

const unavailable = (l: Load<unknown> | null) => l?.status === 'error' && classifyV3Error(l.error).kind === 'unavailable';

export const CAUSES_EXAMPLES = [
  { id: 'Q361', label: 'Première Guerre mondiale' },
  { id: 'Q8698', label: 'Grande Dépression' },
  { id: 'Q160649', label: 'Grippe de 1918' },
];

export default function CausesPage() {
  const [params, setParams] = useSearchParams();
  const event = params.get('event');
  const chainTo = params.get('vers');
  const chainFromParam = params.get('de');
  const chainFrom = chainFromParam ?? event;
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [extra, setExtra] = useState<Map<string, CausalClaim>>(new Map());
  const [showCandidates, setShowCandidates] = useState(false);

  useEffect(() => {
    setSelected(null);
    setShowCandidates(false);
  }, [event]);

  // bring the opened claim into view (edge tap, "détails", "ouvrir")
  useEffect(() => {
    if (!selected) return;
    const el = document.querySelector('.v3-claim-panel');
    if (el && 'scrollIntoView' in el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [selected]);

  const ev = useLoad<{ event: CausalEvent; warnings: string[] }>(event, (s) => getCausalEvent(event!, s));
  const causes = useLoad<ClaimsResult>(event, (s) => getCausalClaims(event!, 'causes', s));
  const effects = useLoad<ClaimsResult>(event, (s) => getCausalClaims(event!, 'effects', s));
  const why = useLoad<WhyResult>(event, (s) => getCausalWhy(event!, s));
  const cands = useLoad<{ candidates: CausalCandidate[]; warnings: string[] }>(event && showCandidates ? `${event}#candidates` : null, (s) => getCausalCandidates(event!, s));

  useEffect(() => {
    if (ev?.status === 'ok' && event) setLabels((l) => ({ ...l, [event]: ev.data.event.label }));
  }, [ev, event]);

  const causeClaims = useMemo(() => (causes?.status === 'ok' ? causes.data.claims : []), [causes]);
  const effectClaims = useMemo(() => (effects?.status === 'ok' ? effects.data.claims : []), [effects]);
  const all = useMemo(() => {
    const m = new Map<string, CausalClaim>(extra);
    const whyClaims = why?.status === 'ok' ? [...Object.values(why.data.groups).flat(), ...why.data.dissent] : [];
    for (const c of [...whyClaims, ...causeClaims, ...effectClaims]) m.set(c.id, c);
    return m;
  }, [causeClaims, effectClaims, why, extra]);

  const focal = ev?.status === 'ok' ? ev.data.event : null;
  const focalLabel = focal?.label ?? (event ? (labels[event] ?? event) : '');
  const graph = useMemo(() => (event ? buildGraph({ id: event, label: focalLabel }, causeClaims, effectClaims) : null), [event, focalLabel, causeClaims, effectClaims]);
  const pairs = useMemo(() => dissentPairs([...all.values()]), [all]);

  const set = useCallback(
    (patch: Record<string, string | null>) => {
      const p = new URLSearchParams(params);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === '') p.delete(k);
        else p.set(k, v);
      }
      setParams(p);
    },
    [params, setParams],
  );

  const pickEvent = (e: PickedEntity | null) => {
    if (e) setLabels((l) => ({ ...l, [e.qid]: e.label }));
    set({ event: e ? e.qid : null, de: null });
  };
  const openNode = (id: string) => set({ event: id, de: null });
  const openClaim = (c: CausalClaim) => {
    setExtra((m) => new Map(m).set(c.id, c));
    setSelected(c.id);
  };

  const screenUnavailable = unavailable(ev) && unavailable(causes) && unavailable(effects);
  const selectedClaim = selected ? (all.get(selected) ?? null) : null;
  const whyGroups = why?.status === 'ok' ? why.data.groups : unavailable(why) && causes?.status === 'ok' ? groupWhy(causeClaims) : null;
  const warnings = [...new Set([...(ev?.status === 'ok' ? ev.data.warnings : []), ...(causes?.status === 'ok' ? causes.data.warnings : []), ...(effects?.status === 'ok' ? effects.data.warnings : [])])];

  return (
    <section className="v3-page v3-causes" aria-labelledby="causes-title">
      <header className="v3-page-head">
        <h1 id="causes-title">Causes &amp; conséquences</h1>
        <p className="v3-lede">Ce que les sources affirment sur les causes et les conséquences d’un événement — qui l’affirme, avec quel niveau de preuve, et qui le conteste.</p>
      </header>
      <p className="v3-banner v3-banner-path" role="note" data-testid="causal-claim-banner">
        <strong>Un lien causal est une affirmation, jamais un fait établi par REVA.</strong> Chaque flèche est attribuée à la source qui l’affirme (« selon … »), avec son niveau de source
        (T1–T4), sa vérification temporelle et un grade de preuve A–E. Succession, simultanéité ou participation ne sont jamais présentées comme des causes.
      </p>
      <form className="v3-path-form" onSubmit={(e) => e.preventDefault()} aria-label="Choix de l’événement">
        <EntityPicker
          label="Événement"
          value={event ? { qid: event, label: focalLabel } : null}
          onChange={pickEvent}
          placeholder="Rechercher un événement (ex. Première Guerre mondiale, Q361)"
          testId="causes-event"
        />
      </form>
      {!event && (
        <div className="v3-state state state-empty" role="status">
          <p>
            <strong>Choisissez un événement.</strong> Exemples :{' '}
            {CAUSES_EXAMPLES.map((x, i) => (
              <span key={x.id}>
                {i > 0 && ' · '}
                <button type="button" className="v3-link-button" onClick={() => pickEvent({ qid: x.id, label: x.label })}>
                  {x.label}
                </button>
              </span>
            ))}
          </p>
        </div>
      )}
      {event && screenUnavailable && <V3Error error={ev!.status === 'error' ? ev!.error : null} what="Le service de causalité" />}
      {event && !screenUnavailable && (
        <>
          {warnings.length > 0 && (
            <ul className="v3-warnings" aria-label="Avertissements du serveur">
              {warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
          <div className="v3-causes-top">
            <div className="v3-causes-event">
              {ev?.status === 'loading' && <V3Loading what="de l’événement" />}
              {ev?.status === 'error' && <V3Error error={ev.error} what="La fiche de l’événement" />}
              {focal && <EventCard e={focal} />}
            </div>
            <div className="v3-causes-graph">
              <h2>Graphe causal gradué</h2>
              {(causes?.status === 'loading' || effects?.status === 'loading') && <V3Loading what="des affirmations causales" />}
              {causes?.status === 'error' && <V3Error error={causes.error} what="Les causes affirmées" />}
              {effects?.status === 'error' && <V3Error error={effects.error} what="Les conséquences affirmées" />}
              {graph && causes?.status !== 'loading' && effects?.status !== 'loading' && (causeClaims.length > 0 || effectClaims.length > 0) && (
                <Suspense fallback={<V3Loading what="du graphe" />}>
                  <CausalGraph
                    model={graph}
                    selected={selected}
                    onSelectClaim={setSelected}
                    onOpenNode={openNode}
                    label={`Graphe causal de ${focalLabel} : ${causeClaims.length} cause(s) et ${effectClaims.length} conséquence(s) affirmées par des sources`}
                  />
                </Suspense>
              )}
              {causes?.status === 'ok' && effects?.status === 'ok' && causeClaims.length === 0 && effectClaims.length === 0 && (
                <p className="v3-missing" data-testid="no-claims">
                  Aucune affirmation causale sourcée sur cet événement dans les données consultées. REVA ne déduit aucune cause de la chronologie.
                </p>
              )}
              <CausalLegend />
            </div>
          </div>
          {selectedClaim && (
            <div className="v3-claim-panel">
              <ClaimDetail c={selectedClaim} all={all} onOpen={setSelected} onClose={() => setSelected(null)} />
            </div>
          )}
          <div className="v3-claim-lists">
            <ClaimListSection title="Causes affirmées" load={causes} claims={causeClaims} show="cause" onOpen={setSelected} testId="causes-list" />
            <ClaimListSection title="Conséquences affirmées" load={effects} claims={effectClaims} show="effect" onOpen={setSelected} testId="effects-list" />
          </div>
          {why?.status === 'loading' && <V3Loading what="de l’explication" />}
          {why?.status === 'error' && !unavailable(why) && <V3Error error={why.error} what="Le panneau « Pourquoi ? »" />}
          {whyGroups && <WhyPanel groups={whyGroups} derived={why?.status !== 'ok'} onOpen={setSelected} />}
          {focal && (causeClaims.length > 0 || effectClaims.length > 0) && <CausalTimeline focal={focal} causes={causeClaims} effects={effectClaims} />}
          <DissentPanel pairs={pairs} onOpen={setSelected} />
          <ChainFinder
            from={chainFrom}
            to={chainTo}
            labels={labels}
            onChange={(p) => {
              const patch: Record<string, string | null> = {};
              if (p.from !== undefined) {
                if (p.from) setLabels((l) => ({ ...l, [p.from!.qid]: p.from!.label }));
                patch.de = p.from ? (p.from.qid === event ? null : p.from.qid) : null;
              }
              if (p.to !== undefined) {
                if (p.to) setLabels((l) => ({ ...l, [p.to!.qid]: p.to!.label }));
                patch.vers = p.to ? p.to.qid : null;
              }
              set(patch);
            }}
            onOpen={openClaim}
            onLabels={(l) => setLabels((old) => ({ ...l, ...old }))}
          />
          <section className="v3-candidates-section" aria-labelledby="cand-title">
            <h2 id="cand-title">Espace des candidats</h2>
            <label className="v3-check">
              <input type="checkbox" checked={showCandidates} onChange={(e) => setShowCandidates(e.target.checked)} data-testid="candidates-toggle" /> Afficher les {CANDIDATE_LABEL}
            </label>
            <p className="field-hint">
              Désactivé par défaut. Les candidats sont des liens proposés par extraction de texte ou par un modèle : ils ne sont affirmés par aucune source, n’ont pas de grade, ne figurent
              ni dans le graphe ni dans « Pourquoi ? », et ne sont jamais utilisés dans les réponses.
            </p>
            {showCandidates && (
              <div className="v3-candidates" data-testid="candidates-panel">
                <p className="v3-candidates-label">
                  <strong>{CANDIDATE_LABEL}</strong>
                </p>
                {cands?.status === 'loading' && <V3Loading what="des candidats" />}
                {cands?.status === 'error' && <V3Error error={cands.error} what="L’espace des candidats" />}
                {cands?.status === 'ok' && <CandidateList items={cands.data.candidates} />}
              </div>
            )}
          </section>
        </>
      )}
    </section>
  );
}

function ClaimListSection({
  title,
  load,
  claims,
  show,
  onOpen,
  testId,
}: {
  title: string;
  load: Load<ClaimsResult> | null;
  claims: CausalClaim[];
  show: 'cause' | 'effect';
  onOpen: (id: string) => void;
  testId: string;
}) {
  if (load?.status !== 'ok') return null;
  return (
    <section className="v3-claim-list" data-testid={testId}>
      <h3>
        {title} <span className="muted">({claims.length})</span>
      </h3>
      {claims.length === 0 ? (
        <p className="muted">Aucune affirmation sourcée.</p>
      ) : (
        <ul className="v3-plain">
          {claims.map((c) => (
            <li key={c.id}>
              <ClaimLine c={c} show={show} onOpen={onOpen} />
            </li>
          ))}
        </ul>
      )}
      {load.data.truncated && <p className="field-hint">Liste bornée par le serveur : d’autres affirmations existent.</p>}
    </section>
  );
}
