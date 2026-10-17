import { useEffect, useState } from 'react';
import { EntityPicker, type PickedEntity } from '../components/EntityPicker';
import { V3Empty, V3Error, V3Loading } from '../components/States';
import { getCausalChain, type CausalClaim, type ChainResult } from './api';
import { chainGrade, hopGrade, weakestGrade } from './model';
import { CHAIN_BANNER, ClaimLine, GradeBadge } from './parts';

export const MAX_CHAIN_HOPS = 6;

type State = { status: 'idle' } | { status: 'loading' } | { status: 'ok'; r: ChainResult } | { status: 'error'; error: unknown };

/**
 * Causal chain finder between two events: only causal claims are followed (context relations never count),
 * each hop shows its own asserting source, and the chain is labelled as REVA's composition of distinct
 * claims whose strength is its weakest link.
 */
export function ChainFinder({
  from,
  to,
  labels,
  onChange,
  onOpen,
  onLabels,
}: {
  from: string | null;
  to: string | null;
  labels: Record<string, string>;
  onChange: (patch: { from?: PickedEntity | null; to?: PickedEntity | null }) => void;
  onOpen: (c: CausalClaim) => void;
  /** labels of the chain ends learned from the response (the pickers show names, not ids) */
  onLabels?: (l: Record<string, string>) => void;
}) {
  const [state, setState] = useState<State>({ status: 'idle' });
  const [hops, setHops] = useState(4);

  useEffect(() => {
    if (!from || !to || from === to) {
      setState({ status: 'idle' });
      return;
    }
    const ctrl = new AbortController();
    setState({ status: 'loading' });
    getCausalChain(from, to, { max_hops: hops }, ctrl.signal)
      .then((r) => {
        if (ctrl.signal.aborted) return;
        setState({ status: 'ok', r });
        const l: Record<string, string> = {};
        for (const n of [r.from, r.to, ...r.chains.flatMap((c) => c.hops.flatMap((h) => [h.from, h.to]))]) if (n && n.id && n.label !== n.id) l[n.id] = n.label;
        if (Object.keys(l).length) onLabels?.(l);
      })
      .catch((error: unknown) => {
        if (!ctrl.signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) setState({ status: 'error', error });
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [from, to, hops]);

  return (
    <section className="v3-chainfinder" aria-labelledby="chain-title" data-testid="chain-finder">
      <h2 id="chain-title">Chaîne causale entre deux événements</h2>
      <div className="v3-path-form">
        <EntityPicker label="De (cause en amont)" value={from ? { qid: from, label: labels[from] ?? from } : null} onChange={(e) => onChange({ from: e })} testId="chain-from" />
        <EntityPicker label="Vers (effet en aval)" value={to ? { qid: to, label: labels[to] ?? to } : null} onChange={(e) => onChange({ to: e })} testId="chain-to" />
        <label>
          Maillons max.
          <select value={hops} onChange={(e) => setHops(Number(e.target.value))}>
            {Array.from({ length: MAX_CHAIN_HOPS }, (_, i) => i + 1).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        {from && to && (
          <button type="button" onClick={() => onChange({ from: { qid: to, label: labels[to] ?? to }, to: { qid: from, label: labels[from] ?? from } })}>
            Inverser
          </button>
        )}
      </div>
      <p className="v3-banner v3-banner-chain" role="note" data-testid="chain-banner">
        <strong>{CHAIN_BANNER.charAt(0).toUpperCase() + CHAIN_BANNER.slice(1)}.</strong> Aucune source n’affirme la chaîne entière : chaque maillon est une affirmation distincte, attribuée à sa
        source ; seules les relations causales sont suivies (au plus {MAX_CHAIN_HOPS} maillons). Les relations de contexte, les issues codées (comment un événement s’est terminé), les
        affirmations attribuées à un tiers ou qualifiées, et celles prises dans une boucle ne sont jamais composées.
      </p>
      {state.status === 'idle' && (
        <V3Empty>
          <p>Choisissez un second événement pour chercher une chaîne d’affirmations causales.</p>
        </V3Empty>
      )}
      {state.status === 'loading' && <V3Loading what="des chaînes causales" />}
      {state.status === 'error' && <V3Error error={state.error} what="La recherche de chaîne causale" />}
      {state.status === 'ok' && <ChainList r={state.r} onOpen={onOpen} />}
    </section>
  );
}

function ChainList({ r, onOpen }: { r: ChainResult; onOpen: (c: CausalClaim) => void }) {
  if (!r.found || r.chains.length === 0) {
    return (
      <V3Empty>
        <p data-testid="chain-none">
          <strong>Aucune chaîne d’affirmations causales</strong>
          {r.maxHops ? ` d’au plus ${r.maxHops} maillons` : ''} dans les données consultées. Cela ne prouve ni l’absence de lien causal, ni son existence.
        </p>
        {r.notFoundReason && <p className="muted">Raison donnée par le serveur : {r.notFoundReason}</p>}
        <SkippedNote r={r} />
        <p className="field-hint" data-testid="chain-none-exclusions">
          Ne sont jamais composés : les relations de contexte, les issues codées (une issue dit comment un événement s’est terminé ; elle figure sur la page de l’événement, pas dans une
          chaîne), les affirmations attribuées à un tiers ou qualifiées par la source, et celles prises dans une boucle.
        </p>
      </V3Empty>
    );
  }
  return (
    <>
      {r.warnings.length > 0 && (
        <ul className="v3-warnings" aria-label="Avertissements du serveur">
          {r.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {r.note && (
        <details className="v3-hop-server">
          <summary>Note du serveur</summary>
          <p>{r.note}</p>
        </details>
      )}
      {r.duplicatesMerged > 0 && (
        <p className="field-hint" data-testid="chain-duplicates">
          {r.duplicatesMerged} chemin(s) identique(s) trouvé(s) en double par la recherche (mêmes maillons, mêmes affirmations, mêmes liens d’identité) : affiché(s) une seule fois.
        </p>
      )}
      <ol className="v3-chain-list">
        {r.chains.map((ch, i) => {
          const grade = chainGrade(ch);
          const causalHops = ch.hops.filter((h) => !h.identity);
          const identityHops = ch.hops.length - causalHops.length;
          const hopsWeakest = weakestGrade(causalHops.map(hopGrade).filter((g): g is NonNullable<typeof g> => g !== null));
          let n = 0;
          return (
            <li key={i} className="v3-causal-chain" data-testid="causal-chain">
              <h3>
                Chaîne {i + 1}{' '}
                <span className="muted">
                  · {causalHops.length} maillon{causalHops.length > 1 ? 's' : ''}
                  {identityHops > 0 ? ` · ${identityHops} lien(s) d’identité` : ''}
                </span>{' '}
                · force (maillon le plus faible) : <GradeBadge grade={grade} />
                {ch.serverGrade && hopsWeakest && ch.serverGrade !== hopsWeakest && (
                  <span className="v3-notice">
                    {' '}
                    grade de chaîne du serveur {ch.serverGrade}, maillon le plus faible {hopsWeakest} : la valeur la plus faible est retenue
                  </span>
                )}
              </h3>
              <ol className="v3-hops">
                {ch.hops.map((h, j) =>
                  h.identity ? (
                    <li key={`${h.from.id}-${h.to.id}-${j}`} className="v3-hop v3-hop-identity" data-testid="chain-identity-hop">
                      <p className="v3-hop-sentence">
                        <span className="muted">Identité :</span> {h.from.label} <span className="v3-claim-arrow">≡</span> {h.to.label} · même événement enregistré par deux sources, relié par un lien{' '}
                        <code>same_as</code> fondé sur une preuve (identifiant partagé ou table de correspondance du fournisseur, jamais par nom ou date) — ce n’est pas une affirmation causale ni
                        un maillon.
                      </p>
                      <ul className="v3-plain">
                        {h.identity.map((x) => (
                          <li key={x.statementKey} className="muted">
                            règle <code>{x.rule}</code>
                            {x.evidenceType ? ` · preuve : ${x.evidenceType}` : ''}
                            {typeof x.evidence.property === 'string' ? ` · ${x.evidence.property}` : ''}
                            {typeof x.evidence.value === 'string' ? ` = ${x.evidence.value}` : ''}
                            {x.independentSources === false ? ' · sources non indépendantes (comptées une fois)' : ''} · déclaration <code>{x.statementKey}</code>
                            {x.note && <span className="v3-identity-note"> — {x.note}</span>}
                          </li>
                        ))}
                      </ul>
                    </li>
                  ) : (
                  <li key={`${h.from.id}-${h.to.id}-${j}`} className="v3-hop" data-testid="chain-hop">
                    <p className="v3-hop-sentence">
                      <span className="muted">Maillon {(n += 1)} :</span> {h.from.label} <span className="v3-claim-arrow">→</span> {h.to.label} · grade du maillon (meilleure affirmation) :{' '}
                      <GradeBadge grade={hopGrade(h)} /> <span className="muted">· {h.claims.length} affirmation(s), chacune avec sa source</span>
                    </p>
                    <ul className="v3-plain">
                      {h.claims.map((c) => (
                        <li key={c.id}>
                          <ClaimLine c={c} onOpen={() => onOpen(c)} />
                        </li>
                      ))}
                    </ul>
                  </li>
                  ),
                )}
              </ol>
              <p className="v3-chain-wording" data-testid="chain-wording">
                {identityHops > 0
                  ? `Composition par REVA de ${causalHops.length} affirmation(s) énoncée(s) séparément, à travers ${identityHops} lien(s) d’identité (même événement dans deux sources) ; aucune source n’affirme cette chaîne.`
                  : `Composition par REVA de ${causalHops.length} affirmation(s) énoncée(s) séparément ; aucune source n’affirme cette chaîne.`}
                {ch.wording && <span className="muted"> Formulation du serveur : {ch.wording}</span>}
              </p>
            </li>
          );
        })}
      </ol>
      {r.truncated && <p className="field-hint">Recherche bornée par le serveur : d’autres chaînes peuvent exister.</p>}
      <SkippedNote r={r} />
    </>
  );
}

/** What the search refused to traverse, as the server counts it (never silently). */
function SkippedNote({ r }: { r: ChainResult }) {
  const s = r.skipped;
  const parts = [
    s.classNodes > 0 ? `${s.classNodes} élément(s) de type classe (« guerre », « traité »…) non traversé(s)${s.classSample.length ? ` : ${s.classSample.slice(0, 5).join(', ')}` : ''}` : '',
    s.quarantined > 0 ? `${s.quarantined} affirmation(s) en boucle, auto-référente(s), ou attribuée(s), qualifiée(s) ou nuancée(s) par Wikidata écartée(s)` : '',
    s.unloaded > 0 ? `${s.unloaded} entité(s) non chargée(s) dans le graphe de connaissances non traversée(s)${s.unloadedSample.length ? ` : ${s.unloadedSample.slice(0, 5).join(', ')}` : ''}` : '',
    s.belowMinGrade > 0 ? `${s.belowMinGrade} affirmation(s) sous le grade minimal${r.minGrade ? ` ${r.minGrade}` : ''} écartée(s)` : '',
    s.ungraded > 0 ? `${s.ungraded} affirmation(s) non gradée(s) écartée(s)` : '',
  ].filter(Boolean);
  if (parts.length === 0) return null;
  return (
    <p className="field-hint" data-testid="chain-skipped">
      Écartés par la recherche : {parts.join(' ; ')}.
    </p>
  );
}
