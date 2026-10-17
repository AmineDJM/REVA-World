import { useContext } from 'react';
import { Link } from 'react-router';
import { CitationChip } from '../components/CitationChip';
import { FixtureBadge } from '../components/Honesty';
import { LicenceBadge } from '../components/Licence';
import { wikidataItemUrl } from '../lib/citations';
import { kindLabel } from '../lib/kinds';
import type { CausalCandidate, CausalClaim, CausalEvent, ComparedTime, EventsCard, Grade, Tier, WhyGroup } from './api';
import { GRADES, isOutcomeClaim } from './api';
import { RightsContext } from './rights';
import {
  attributedTo,
  citeClaim,
  FAMILIES,
  FAMILY_STYLE,
  familyOf,
  GRADE_STYLE,
  intervalText,
  partiesFr,
  precisionFr,
  relationFr,
  REVIEW_FR,
  selon,
  sourceName,
  TEMPORAL_FR,
  TIER_FR,
  timeText,
  UNGRADED_STYLE,
  WHY_FR,
  type DissentPair,
} from './model';

export const causesHref = (id: string) => `/v3/causes?event=${encodeURIComponent(id)}`;

export const CANDIDATE_LABEL = 'non sourcé — candidats (extraits de textes ou proposés par un modèle, non vérifiés)';
export const CHAIN_BANNER = 'chaîne composée par REVA à partir d’affirmations distinctes — force = maillon le plus faible';

// ------------------------------------------------------------------ badges

export function GradeBadge({ grade, rules }: { grade: Grade | null; rules?: string[] }) {
  if (grade === null) {
    return (
      <span className="v3-grade v3-grade-none" title={UNGRADED_STYLE.rule} data-testid="grade-badge" data-grade="none">
        non gradée
      </span>
    );
  }
  const title = `Grade de preuve ${grade} : ${GRADE_STYLE[grade].rule}${rules && rules.length ? ` — règles : ${rules.join(', ')}` : ''}`;
  return (
    <span className={`v3-grade v3-grade-${grade}`} title={title} data-testid="grade-badge" data-grade={grade}>
      preuve {grade}
    </span>
  );
}

export function TierBadge({ tier }: { tier: Tier }) {
  return (
    <span className={`v3-tier v3-tier-${tier}`} title={TIER_FR[tier]} data-testid="tier-badge">
      {tier}
    </span>
  );
}

export function TemporalBadge({ status, detail }: { status: string; detail?: string | null }) {
  const t = TEMPORAL_FR[status] ?? TEMPORAL_FR.unknown!;
  return (
    <span className={`v3-tcheck v3-tcheck-${t.tone}`} data-testid="temporal-badge" data-temporal={status} title={detail ?? undefined}>
      {t.label}
    </span>
  );
}

export function FamilySwatch({ family }: { family: (typeof FAMILIES)[number] }) {
  const s = FAMILY_STYLE[family];
  return (
    <svg width="34" height="10" aria-hidden="true" className="v3-fam-swatch">
      <line x1="1" y1="5" x2="33" y2="5" stroke={s.color} strokeWidth="3" strokeDasharray={s.line === 'dashed' ? '7 4' : s.line === 'dotted' ? '1.5 4' : undefined} strokeLinecap="round" />
    </svg>
  );
}

/** Legend: relation families (colour/dash) and grades (thickness/opacity), with the grading rule. */
export function CausalLegend() {
  return (
    <div className="v3-causal-legend" data-testid="causal-legend">
      <div>
        <h4>Famille de relation (selon la source)</h4>
        <ul>
          {FAMILIES.map((f) => (
            <li key={f}>
              <FamilySwatch family={f} /> {FAMILY_STYLE[f].label}
            </li>
          ))}
        </ul>
      </div>
      <div>
        <h4>Grade de preuve (ordinal, sans probabilité)</h4>
        <ul>
          {GRADES.map((g) => (
            <li key={g}>
              <svg width="34" height="10" aria-hidden="true">
                <line x1="1" y1="5" x2="33" y2="5" stroke="#3b4a66" strokeWidth={GRADE_STYLE[g].width} strokeOpacity={GRADE_STYLE[g].opacity} />
              </svg>{' '}
              <strong>{g}</strong> — {GRADE_STYLE[g].rule}
            </li>
          ))}
        </ul>
        <p className="field-hint">Grade calculé par des règles déclarées (registre de notation), affiché avec les règles appliquées. Un grade n’est pas une probabilité.</p>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ claims

function NodeLink({ id, label }: { id: string; label: string }) {
  return (
    <Link to={causesHref(id)} className="v3-causal-node-link">
      {label}
    </Link>
  );
}

/** The focal event's side of a one-line claim: plain text (it is the event on screen), never a link. */
function Focal({ label }: { label: string }) {
  return <span className="v3-claim-focal">{label}</span>;
}

/** Licence of the claim's source, when the registry marks it restrictive (non-commercial, not open…). */
function ClaimLicence({ c }: { c: Pick<CausalClaim, 'source'> }) {
  const rights = useContext(RightsContext);
  // the causal API carries the source's licence (audit2 N9); the registry map is the fallback for older servers
  const r = c.source.rights ?? rights?.get(c.source.key);
  return r ? <LicenceBadge rights={r} /> : null;
}

/** Attribution / nature / doubtful-coding notes of a claim (grading v2), as short badges. */
function AttributionBadges({ c }: { c: CausalClaim }) {
  const a = c.attribution;
  if (!a) return null;
  return (
    <>
      {a.supportedBy.length > 0 && (
        <span className="v3-attrib" data-testid="claim-attributed" title={attributedTo(c) ?? undefined}>
          attribuée à {partiesFr(a.supportedBy)} — {sourceName(c)} ne l’affirme pas lui-même
        </span>
      )}
      {a.nature.length > 0 && (
        <span className="v3-attrib" data-testid="claim-nature">
          nature qualifiée par la source : {partiesFr(a.nature)}
        </span>
      )}
      {a.hedges.length > 0 && (
        <span className="v3-attrib" data-testid="claim-hedged">
          Wikidata qualifie : {partiesFr(a.hedges)} (circonstances de la source)
        </span>
      )}
      {a.scope.length > 0 && (
        <span className="v3-attrib" data-testid="claim-scope">
          portée restreinte par la source : {partiesFr(a.scope)}
        </span>
      )}
      {a.doubtful.length > 0 && (
        <span className="v3-attrib" data-testid="claim-doubtful">
          codé douteux par la source
        </span>
      )}
    </>
  );
}

/**
 * One-line attributed claim, always in the source's direction: "cause — relation → effet · selon <source> (T1) ·
 * preuve B". The event on screen (`show` names the *other* end) is plain text, the other end a link.
 */
export function ClaimLine({ c, onOpen, show = 'both' }: { c: CausalClaim; onOpen?: (id: string) => void; show?: 'cause' | 'effect' | 'both' }) {
  const fam = familyOf(c);
  const outcome = isOutcomeClaim(c);
  return (
    <span className={`v3-claim-line${outcome ? ' v3-claim-outcome' : ''}`} data-testid="claim-line" data-claim={c.id} data-relation={c.relation}>
      <FamilySwatch family={fam} /> {show === 'effect' ? <Focal label={c.cause.label} /> : <NodeLink id={c.cause.id} label={c.cause.label} />}
      {c.stance === 'denies' ? <span className="v3-denies"> — lien nié ({relationFr(c)}) → </span> : <span className="muted"> — {relationFr(c)} → </span>}
      {show === 'cause' ? <Focal label={c.effect.label} /> : <NodeLink id={c.effect.id} label={c.effect.label} />}
      {outcome && <span className="v3-outcome-tag"> issue, pas une cause</span>}{' '}
      <span className="v3-selon" data-testid="selon">
        {selon(c)}
      </span>
      {c.source.property && <span className="muted"> · {c.source.property}</span>} <AttributionBadges c={c} />
      <GradeBadge grade={c.grade} rules={c.gradeRules} /> <ClaimLicence c={c} />
      {c.temporal.status === 'inconsistent' && <span className="v3-tcheck v3-tcheck-bad v3-tcheck-mini">incohérence temporelle</span>}
      {(c.contestedBy.length > 0 || c.contests.length > 0) && <span className="v3-contested-mini">contestée</span>}
      {onOpen && (
        <button type="button" className="v3-link-button" onClick={() => onOpen(c.id)} aria-label={`Détails de l’affirmation ${c.cause.label} → ${c.effect.label} ${selon(c)}`}>
          détails
        </button>
      )}
    </span>
  );
}

const shortDay = (iso: string | null) => (iso ? timeText({ time: iso, precision: 11 }) : null);

/** The time an endpoint was compared by: an event start (with precision), or an entity's existence span. */
function comparedText(n: CausalClaim['cause'], t: ComparedTime | null, entity: boolean): string {
  if (entity && t && t.kind === 'entity_existence') {
    const a = shortDay(t.inception ?? t.from);
    const b = t.end ? shortDay(t.end) : null;
    return `entité existant ${b ? `de ${a} à ${b}` : `depuis ${a} (fin non indiquée)`} — datée par sa création (P571), pas un événement`;
  }
  const when = intervalText(n);
  return when ? `${when}${n.start?.precision != null ? ` (${precisionFr(n.start.precision)})` : ''}` : 'non daté';
}

/** Full claim card: everything a reader needs to judge it, and nothing REVA asserts itself. */
export function ClaimDetail({ c, all, onOpen, onClose }: { c: CausalClaim; all: Map<string, CausalClaim>; onOpen: (id: string) => void; onClose?: () => void }) {
  const fam = familyOf(c);
  const outcome = isOutcomeClaim(c);
  const attributed = attributedTo(c);
  const rights = useContext(RightsContext);
  const licence = c.source.rights ?? rights?.get(c.source.key) ?? null;
  return (
    <article className="v3-claim-detail" data-testid="claim-detail" aria-label={`Affirmation ${c.cause.label} → ${c.effect.label}`}>
      <header>
        <h3>
          <NodeLink id={c.cause.id} label={c.cause.label} /> <span className="v3-claim-arrow">→</span> <NodeLink id={c.effect.id} label={c.effect.label} />
        </h3>
        {onClose && (
          <button type="button" className="v3-link-button" onClick={onClose}>
            fermer
          </button>
        )}
      </header>
      <p className="v3-claim-assert" data-testid="claim-attribution">
        {c.stance === 'denies' ? (
          <strong>Lien nié {selon(c)}</strong>
        ) : attributed ? (
          <strong>
            {selon(c).replace(/^selon/, 'Selon')}, affirmation attribuée à {partiesFr(c.attribution!.supportedBy)}
          </strong>
        ) : outcome ? (
          <strong>Issue codée {selon(c)}</strong>
        ) : (
          <strong>Affirmé {selon(c)}</strong>
        )}{' '}
        : {relationFr(c)}
        {!FAMILY_STYLE[fam].label.startsWith(relationFr(c)) && <span className="muted"> ({FAMILY_STYLE[fam].label})</span>}
        {c.polarity && <span className="muted"> · polarité : {c.polarity === 'inhibits' ? 'inhibe' : c.polarity === 'promotes' ? 'favorise' : c.polarity}</span>}.{' '}
        {attributed && <span data-testid="claim-attributed-text">{attributed}. </span>}
        {outcome && (
          <span data-testid="claim-outcome-text">
            Le jeu de données code ici comment l’événement s’est terminé (son issue) : ce n’est ni une cause ni une conséquence, et ce lien n’est jamais composé dans une chaîne causale.{' '}
          </span>
        )}
        REVA n’affirme aucune causalité : il rapporte cette affirmation.
      </p>
      <dl className="v3-dl v3-claim-dl">
        <dt>Niveau de source</dt>
        <dd>
          <TierBadge tier={c.tier} /> {TIER_FR[c.tier]}
        </dd>
        <dt>Source affirmante</dt>
        <dd>
          {sourceName(c)} <CitationChip c={citeClaim(c)} />
        </dd>
        <dt>Licence de la source</dt>
        <dd data-testid="claim-licence">
          {licence ? (
            <>
              {licence.licence_url && /^https:\/\//.test(licence.licence_url) ? (
                <a href={licence.licence_url} target="_blank" rel="noreferrer noopener">
                  {licence.licence}
                </a>
              ) : (
                licence.licence
              )}{' '}
              <LicenceBadge rights={licence} />
            </>
          ) : (
            <span className="muted">{rights ? 'non indiquée dans le registre des sources' : 'non indiquée par le service causal'}</span>
          )}
        </dd>
        <dt>Déclaration / ligne</dt>
        <dd data-testid="claim-statement">
          {c.source.statementId ? <code>{c.source.statementId}</code> : c.source.rowSelector ? <code>{c.source.rowSelector}</code> : <span className="v3-missing">non fournie par le serveur</span>}
          {c.source.property && <span className="muted"> · propriété {c.source.property}</span>}
          {c.assertedOn && <span className="muted"> · écrite sur {c.assertedOn === 'cause' ? 'la cause' : 'l’effet'} ({c.assertedOn === 'cause' ? c.cause.label : c.effect.label})</span>}
          {c.source.revision ? (
            <span>
              {' '}
              · révision <code>{c.source.revision}</code>
            </span>
          ) : (
            <span className="muted"> · révision non fournie</span>
          )}
        </dd>
        {c.source.codebook && (
          <>
            <dt>Codebook</dt>
            <dd>{c.source.codebook}</dd>
          </>
        )}
        {c.source.citation && (
          <>
            <dt>Référence citée</dt>
            <dd>
              <q className="v3-verbatim">{c.source.citation}</q>
            </dd>
          </>
        )}
        {c.attribution && (c.attribution.nature.length > 0 || c.attribution.doubtful.length > 0 || c.attribution.note) && (
          <>
            <dt>Qualification par la source</dt>
            <dd data-testid="claim-qualification">
              {c.attribution.nature.length > 0 && <span>Nature de la déclaration selon {sourceName(c)} (citée telle quelle, non interprétée) : {partiesFr(c.attribution.nature)}. </span>}
              {c.attribution.doubtful.map((d, i) => (
                <span key={i}>
                  La source code cet enregistrement comme douteux{d.field ? ` (${d.field}` : ''}
                  {d.code ? `${d.field ? ' = ' : ' ('}${d.code}` : ''}
                  {d.field || d.code ? ')' : ''}
                  {d.label ? ` : « ${d.label} »` : ''}.{' '}
                </span>
              ))}
              {c.attribution.note && c.attribution.nature.length === 0 && <span className="muted">{c.attribution.note}</span>}
              {c.gradeRules.some((r) => /^grade\.D\.(attributed|nature_qualified|source_doubtful)$/.test(r)) && <span className="muted"> Grade plafonné à D par la grille de notation.</span>}
            </dd>
          </>
        )}
        <dt>Mécanisme</dt>
        <dd data-testid="claim-mechanism">
          {c.mechanism ? (
            <blockquote className="v3-verbatim" lang={c.mechanism.lang ?? undefined}>
              « {c.mechanism.text} » <span className="muted">— cité tel quel depuis la source, sans reformulation</span>
            </blockquote>
          ) : (
            <span className="muted">aucun mécanisme énoncé par la source</span>
          )}
        </dd>
        {c.scope && Object.keys(c.scope).filter((k) => k !== 'source_validity').length > 0 && (
          <>
            <dt>Portée (selon la source)</dt>
            <dd>
              {Object.entries(c.scope)
                .filter(([k]) => k !== 'source_validity')
                .map(([k, v]) => `${k} : ${typeof v === 'string' ? v : JSON.stringify(v)}`)
                .join(' · ')}
            </dd>
          </>
        )}
        <dt>Vérification temporelle</dt>
        <dd data-testid="claim-temporal">
          <TemporalBadge status={c.temporal.status} detail={c.temporal.detail} />
          {c.temporal.detail && (
            <span className="v3-temporal-reason" data-testid="temporal-reason">
              {' '}
              — {c.temporal.detail}
            </span>
          )}
          <span className="muted">
            {' '}
            · cause : {comparedText(c.cause, c.temporal.causeTime, c.temporal.entityDated.includes('cause'))} · effet :{' '}
            {comparedText(c.effect, c.temporal.effectTime, c.temporal.entityDated.includes('effect'))}
          </span>
        </dd>
        <dt>Corroboration</dt>
        <dd data-testid="claim-corroboration">
          {c.corroboration.count === null ? (
            <span className="muted">non fournie{c.grade === null ? ' (affirmation pas encore gradée)' : ''}</span>
          ) : (
            <>
              {c.corroboration.count} groupe{c.corroboration.count > 1 ? 's' : ''} de sources indépendant{c.corroboration.count > 1 ? 's' : ''} affirme{c.corroboration.count > 1 ? 'nt' : ''} ce lien
              <span className="muted"> (des sources partageant une même origine comptent une fois)</span>
              {c.corroboration.claims.length > 0 && <span className="muted"> · affirmations concordantes : {c.corroboration.claims.join(', ')}</span>}
            </>
          )}
        </dd>
        <dt>Contestée par</dt>
        <dd data-testid="claim-contested">
          {c.contestedBy.length === 0 ? (
            <span className="muted">aucune affirmation contraire enregistrée</span>
          ) : (
            <ul className="v3-plain">
              {c.contestedBy.map((id) => {
                const o = all.get(id);
                return (
                  <li key={id}>
                    {o ? (
                      <>
                        <ClaimLine c={o} />{' '}
                        <button type="button" className="v3-link-button" onClick={() => onOpen(id)}>
                          ouvrir
                        </button>
                      </>
                    ) : (
                      <span>
                        affirmation <code>{id}</code> <span className="muted">(non chargée sur cet écran)</span>
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </dd>
        <dt>Grade de preuve</dt>
        <dd data-testid="claim-grade">
          <GradeBadge grade={c.grade} rules={c.gradeRules} /> {c.grade ? GRADE_STYLE[c.grade].rule : UNGRADED_STYLE.rule}
          <span className="muted"> · règles appliquées : {c.gradeRules.length ? c.gradeRules.join(', ') : 'non fournies'}</span>
          {c.gradeVersion && <span className="muted"> · grille version {c.gradeVersion}</span>}
          {c.gradeReasons.length > 0 && (
            <ul className="v3-grade-reasons">
              {c.gradeReasons.map((r, i) => (
                <li key={`${r.rule}-${i}`}>
                  <code>{r.rule}</code> {r.text}
                </li>
              ))}
            </ul>
          )}
        </dd>
        <dt>Relecture</dt>
        <dd>{c.reviewStatus ? (REVIEW_FR[c.reviewStatus] ?? c.reviewStatus) : 'non indiquée'}</dd>
      </dl>
      {c.serverWording && (
        <p className="v3-server-wording" data-testid="claim-wording">
          <span className="muted">Formulation du serveur : </span>
          {c.serverWording}
        </p>
      )}
    </article>
  );
}

// ------------------------------------------------------------------ event card

/**
 * Event card: identity, type, dates at their precision, places and participants — from the EVENTS card
 * (/v1/events/{id}, each fact from a cited statement) when deployed, else from the causal event view — plus the
 * counts of claims and the non-causal context of /v1/causal/events/{id}.
 */
export interface LoadedCounts {
  causes: number;
  effects: number;
  outcomes: number;
}

export function EventCard({ e, card, loaded, fallbackLabel }: { e: CausalEvent | null; card: EventsCard | null; loaded?: LoadedCounts | null; fallbackLabel?: string | null }) {
  const id = card?.id ?? e?.id ?? '';
  const wd = wikidataItemUrl(id);
  // the KG has no entity for this id (only claims cite it): say so instead of showing a bare id as a name
  const unknownToKg = !card && !!e && e.label === e.id && !e.type && !e.start && !e.end;
  const title = card?.label ?? (e && e.label !== e.id ? e.label : null) ?? fallbackLabel ?? id;
  const dated = card?.start || card?.end ? card : e;
  const when = dated ? intervalText(dated) : '';
  const typeText = card?.types.length ? card.types.join(' · ') : e ? (e.typeLabel ?? (e.type ? kindLabel(e.type) : null)) : null;
  const places = card ? card.places.map((p) => ({ label: p.ref.label, note: p.role })) : (e?.places ?? []).map((p) => ({ label: p.label, note: null as string | null }));
  const people = card
    ? card.participants.map((p) => ({ key: p.ref.id, label: p.ref.label, note: [p.role, p.side && `camp ${p.side}`].filter(Boolean).join(', ') }))
    : (e?.participants ?? []).map((p) => ({ key: p.id, label: p.label, note: [p.role, p.side].filter(Boolean).join(', ') }));
  const given = card ? true : !!e?.placesGiven;
  const peopleGiven = card ? true : !!e?.participantsGiven;
  return (
    <div className="v3-entity-card v3-event-card" data-testid="event-card">
      <h2 className="v3-entity-title">{title}</h2>
      <p className="v3-entity-sub">
        <span className="v3-chip" data-testid="event-type">
          {typeText ?? 'type non indiqué'}
        </span>{' '}
        {wd ? (
          <a href={wd} target="_blank" rel="noreferrer noopener">
            {id}
          </a>
        ) : (
          <code>{id}</code>
        )}
        {e?.origin === 'test_fixture' && <FixtureBadge />}
      </p>
      {(card?.description ?? e?.description) && <p className="v3-entity-desc">{card?.description ?? e?.description}</p>}
      {unknownToKg && (
        <p className="v3-missing" data-testid="event-not-in-kg">
          Cet identifiant n’est pas une entité du graphe de connaissances chargé : seules des affirmations causales le citent. Ni type, ni dates, ni lieux ne sont disponibles ici.
        </p>
      )}
      <dl className="v3-dl">
        <dt>Dates</dt>
        <dd data-testid="event-dates">
          {when && dated ? (
            <>
              {when}{' '}
              <span className="muted">
                ({dated.start ? `début ${precisionFr(dated.start.precision)}` : 'début non daté'}
                {dated.end ? ` · fin ${precisionFr(dated.end.precision)}` : ''})
              </span>
              {card?.dateConsistency === 'inconsistent' && <span className="v3-tcheck v3-tcheck-bad v3-tcheck-mini">dates contradictoires dans les sources</span>}
            </>
          ) : (
            <span className="v3-undated">non daté dans la source</span>
          )}
        </dd>
        <dt>Lieux</dt>
        <dd data-testid="event-places">
          {places.length ? (
            places.map((p) => p.label + (p.note ? ` (${p.note})` : '')).join(' · ')
          ) : (
            <span className="muted">{given ? 'aucun lieu déclaré dans la source' : 'non fournis par ce service'}</span>
          )}
        </dd>
        <dt>Participants</dt>
        <dd data-testid="event-participants">
          {people.length ? (
            <ul className="v3-plain">
              {people.slice(0, 16).map((p, i) => (
                <li key={`${p.key}-${p.note}-${i}`}>
                  {p.label}
                  {p.note && <span className="muted"> — {p.note}</span>}
                </li>
              ))}
              {people.length > 16 && <li className="muted">… {people.length - 16} autre(s)</li>}
            </ul>
          ) : (
            <span className="muted">{peopleGiven ? 'aucun participant déclaré dans la source' : 'non fournis par ce service'}</span>
          )}
        </dd>
        {card && (
          <>
            <dt>Sources</dt>
            <dd className="muted">fiche d’événement : chaque date, lieu et participant provient d’une déclaration citée (contexte, jamais une cause)</dd>
          </>
        )}
        {e && (e.nCauses !== null || e.nEffects !== null) && (
          <>
            <dt>Affirmations</dt>
            <dd data-testid="event-counts">
              {loaded ? (
                <>
                  {loaded.causes} cause(s) · {loaded.effects} conséquence(s) affirmées
                  {loaded.outcomes > 0 ? ` · ${loaded.outcomes} issue(s) codée(s) (comment l’événement s’est terminé, pas des conséquences)` : ''}
                </>
              ) : (
                <>
                  {e.nCauses ?? '?'} cause(s) · {e.nEffects ?? '?'} conséquence(s) affirmées
                  {e.nOutcomes > 0 ? ` · ${e.nOutcomes} issue(s) codée(s) (comment l’événement s’est terminé, pas des conséquences)` : ''}
                </>
              )}
              {e.nCandidates !== null ? ` · ${e.nCandidates} candidat(s) en quarantaine` : ''}
              {loaded && e.nEffects !== null && e.nEffects + e.nOutcomes !== loaded.effects + loaded.outcomes && (
                <span className="muted"> (décompte du serveur : {e.nCauses ?? '?'} / {e.nEffects} ; les listes ci-dessous sont bornées)</span>
              )}
            </dd>
          </>
        )}
        {e && e.context.length > 0 && (
          <>
            <dt>Contexte (non causal)</dt>
            <dd data-testid="event-context">
              <ul className="v3-plain">
                {e.context.slice(0, 12).map((c, i) => (
                  <li key={`${c.other.id}-${i}`}>
                    {c.eventIsSubject ? `${c.relation} : ${c.other.label}` : `${c.other.label} — ${c.relation}`}
                    <span className="muted"> — relation de contexte, pas une cause</span>
                  </li>
                ))}
              </ul>
              {(e.context.length > 12 || e.contextTruncated) && <span className="muted">liste bornée</span>}
            </dd>
          </>
        )}
      </dl>
    </div>
  );
}

// ------------------------------------------------------------------ why

export function WhyPanel({ groups, derived, onOpen }: { groups: Record<WhyGroup, CausalClaim[]>; derived: boolean; onOpen: (id: string) => void }) {
  const order: WhyGroup[] = ['immediate', 'underlying', 'contributing', 'coded', 'general', 'reaction', 'prevention'];
  const total = order.reduce((n, g) => n + groups[g].length, 0);
  const outcomes = groups.outcome ?? [];
  const generic = groups.generic ?? [];
  return (
    <section className="v3-why" aria-labelledby="why-title" data-testid="why-panel">
      <h2 id="why-title">Pourquoi ?</h2>
      <p className="field-hint">
        Causes affirmées par des sources, classées par la relation que chaque source déclare. Chaque ligne nomme sa source ; REVA ne choisit pas « la » cause.
        {derived && ' Le service « pourquoi » n’étant pas encore disponible, ce regroupement reprend les affirmations ci-dessus selon leur relation déclarée.'}
      </p>
      {total === 0 ? (
        <p className="v3-missing" data-testid="why-empty">
          Aucune source chargée n’affirme de cause pour cet événement — ce n’est pas une absence de cause, c’est une absence d’affirmation.
        </p>
      ) : (
        order
          .filter((g) => groups[g].length > 0)
          .map((g) => (
            <div key={g} className={`v3-why-lane v3-why-${g}`} data-testid={`why-${g}`}>
              <h3>
                {WHY_FR[g]} <span className="muted">({groups[g].length})</span>
              </h3>
              <ul className="v3-plain">
                {groups[g].map((c) => (
                  <li key={c.id}>
                    <ClaimLine c={c} show="cause" onOpen={onOpen} />
                    {c.mechanism && <q className="v3-verbatim v3-why-quote">{c.mechanism.text}</q>}
                  </li>
                ))}
              </ul>
            </div>
          ))
      )}
      {generic.length > 0 && (
        <div className="v3-why-lane v3-why-generic" data-testid="why-generic">
          <h3>
            {WHY_FR.generic} <span className="muted">({generic.length})</span>
          </h3>
          <p className="field-hint">La cause ou l’effet de ces affirmations est une classe d’après le graphe de connaissances (sous-classe de, ou seulement « nature de » d’autres éléments) : elles parlent d’un type d’événement en général. Elles ne sont pas comptées parmi les causes de cet événement.</p>
          <ul className="v3-plain">
            {generic.map((c) => (
              <li key={c.id}>
                <ClaimLine c={c} show="cause" onOpen={onOpen} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {outcomes.length > 0 && (
        <div className="v3-why-lane v3-why-outcome" data-testid="why-outcome">
          <h3>
            {WHY_FR.outcome} <span className="muted">({outcomes.length})</span>
          </h3>
          <p className="field-hint">Cet enregistrement est l’issue codée d’un événement : il dit comment cet événement s’est terminé selon le jeu de données. Ce n’est pas une cause, et ce n’est pas compté parmi les causes.</p>
          <ul className="v3-plain">
            {outcomes.map((c) => (
              <li key={c.id}>
                <ClaimLine c={c} show="cause" onOpen={onOpen} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ dissent

export function DissentPanel({ pairs, onOpen }: { pairs: DissentPair[]; onOpen: (id: string) => void }) {
  return (
    <section className="v3-dissent" aria-labelledby="dissent-title" data-testid="dissent-panel">
      <h2 id="dissent-title">Désaccords entre sources</h2>
      <p className="field-hint">Affirmations contestées, côte à côte. REVA ne tranche pas : chaque position reste attribuée à sa source.</p>
      {pairs.length === 0 ? (
        <p className="muted">Aucune affirmation contestée parmi celles affichées.</p>
      ) : (
        pairs.map((p) => (
          <div key={p.claim.id} className="v3-dissent-pair" data-testid="dissent-pair">
            <div className="v3-dissent-side">
              <h4>Affirmation</h4>
              <ClaimLine c={p.claim} onOpen={onOpen} />
            </div>
            <div className="v3-dissent-side v3-dissent-against">
              <h4>Contestée par</h4>
              <ul className="v3-plain">
                {p.against.map((a) => (
                  <li key={a.id}>
                    {a.claim ? (
                      <ClaimLine c={a.claim} onOpen={onOpen} />
                    ) : (
                      <span>
                        affirmation <code>{a.id}</code> <span className="muted">(non chargée sur cet écran)</span>
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        ))
      )}
    </section>
  );
}

// ------------------------------------------------------------------ candidates

export function CandidateList({ items }: { items: CausalCandidate[] }) {
  if (items.length === 0) return <p className="muted">Aucun candidat (non sourcé) n’a été extrait pour cet événement.</p>;
  return (
    <ul className="v3-plain v3-candidate-list">
      {items.map((c) => (
        <li key={c.id} className="v3-candidate" data-testid="candidate">
          <p>
            <span className="v3-candidate-tag">candidat {c.tier}</span> {c.cause.label} <span className="v3-claim-arrow">→</span> {c.effect.label}{' '}
            <span className="muted">· {relationFr(c)} (proposé par extraction, non affirmé par une source)</span>
          </p>
          {c.sentence && (
            <blockquote className="v3-verbatim">
              « {c.sentence} » <span className="muted">— phrase source exacte</span>
            </blockquote>
          )}
          <p className="muted">
            extracteur {c.extractor ?? 'non indiqué'}
            {c.extractorVersion ? ` ${c.extractorVersion}` : ''} · révision {c.sourceRevision ?? 'non indiquée'} · statut : {c.promotionStatus === 'not_promoted' ? 'non promu' : c.promotionStatus} · aucun
            grade (hors de l’espace des affirmations)
          </p>
          {c.source && <CitationChip c={citeClaim(c)} compact />}
        </li>
      ))}
    </ul>
  );
}
