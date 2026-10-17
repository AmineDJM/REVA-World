import { useMemo, useState } from 'react';
import { Link } from 'react-router';
import { v8Links } from '../../contract';
import { Badge, SourceChip, displayValue } from '../../shell/ui';
import type { AnswerBundle, Evidence, Sentence } from './api';
import { EvidenceView } from './EvidenceView';
import { brainstormHref, TARGET_LABEL, targetHref } from './links';
import { evidenceKindFr, evidenceSource } from './provenance';

const STATUS: Record<AnswerBundle['status'], { label: string; kind: 'win' | 'neutral' | 'missing'; title: string }> = {
  answered: { label: 'Réponse sourcée', kind: 'win', title: 'Chaque phrase cite des données chargées.' },
  partial: { label: 'Réponse partielle', kind: 'neutral', title: 'Seules des données voisines, non équivalentes, sont chargées.' },
  unknown: { label: 'REVA ne sait pas', kind: 'missing', title: 'Rien de chargé ne répond : la réponse dit ce qui manque.' },
};

const HONESTY: Record<string, { kind: 'observed' | 'projection' | 'estimate' | 'scenario' | 'judged' | 'community' | 'info'; label?: string }> = {
  observed: { kind: 'observed' },
  projection: { kind: 'projection' },
  forecast: { kind: 'judged', label: 'prévision scellée' },
  scenario: { kind: 'scenario' },
  estimate: { kind: 'estimate', label: 'estimations attribuées' },
  claim: { kind: 'info', label: 'affirmations attribuées' },
  path: { kind: 'info', label: 'connexion, pas une cause' },
  community: { kind: 'community' },
};

const SENTENCE_KIND: Record<string, string> = {
  identification: 'identification',
  missing: 'manquant',
  unavailable: 'indisponible',
  unsupported: 'non pris en charge',
  ambiguous: 'ambigu',
  note: 'note',
  related: 'voisine',
  causal: 'affirmation',
  scenario: 'scénario',
  estimate: 'estimation',
  forecast: 'prévision',
};

export interface AnswerCardProps {
  bundle: AnswerBundle;
  onAsk: (q: string) => void;
  onPin?: () => Promise<string | null>;
}

export function AnswerCard({ bundle, onAsk, onPin }: AnswerCardProps) {
  const ev = useMemo(() => new Map(bundle.evidence.map((e) => [e.id, e])), [bundle.evidence]);
  const [focus, setFocus] = useState<string | null>(null);
  const [pinMsg, setPinMsg] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const st = STATUS[bundle.status];
  const h = bundle.headline;
  const cite = (id: string) => {
    setFocus(id);
    try {
      document.getElementById(`v8d-ev-${id}`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } catch {
      /* jsdom */
    }
  };
  const headSource = h.citations[0] ? ev.get(h.citations[0]) : undefined;
  const hon = h.honesty ? HONESTY[h.honesty] : undefined;
  const body = bundle.sentences.filter((s) => s.kind !== 'sources');
  const sourcesSentence = bundle.sentences.find((s) => s.kind === 'sources');

  return (
    <article className={`v8d-answer v8d-${bundle.status}`} aria-live="polite" data-testid="v8d-answer">
      <header className="v8d-answer-head">
        <Badge kind={st.kind} title={st.title}>
          {st.label}
        </Badge>
        {hon && (
          <Badge kind={hon.kind} title={undefined}>
            {hon.label}
          </Badge>
        )}
        <span className="v8-subtle v8d-meta">
          {bundle.cached ? 'en cache · ' : ''}
          {bundle.latency_ms !== null && bundle.latency_ms !== undefined ? `${Math.round(bundle.latency_ms)} ms` : ''}
          {' · '}
          {bundle.how.evidence_count} preuve(s)
        </span>
      </header>

      <div className="v8d-headline v8d-reveal" style={{ ['--i' as string]: 0 }}>
        {h.kind === 'number' && h.value !== null ? (
          <div className="v8d-big">
            <div className="v8d-big-label">{h.concept_label ?? h.concept ?? ''}{h.iso3 ? ` — ${h.iso3}` : ''}{h.period ? `, ${h.period}` : ''}</div>
            <div className="v8d-big-row">
              <span className={`v8-num v8d-big-num${h.honesty === 'forecast' || h.honesty === 'projection' ? ' is-proj' : ''}`}>{displayValue(h.value, 3)}</span>
              {h.unit && <span className="v8d-big-unit">{h.unit}</span>}
              {headSource && <SourceChip {...evidenceSource(headSource)} />}
            </div>
            {h.band && typeof h.band[0] === 'string' && typeof h.band[1] === 'string' && (
              <div className="v8d-big-sub v8-subtle">
                intervalle 5–95 % : {displayValue(h.band[0], 3)} à {displayValue(h.band[1], 3)}
                {h.model ? ` · modèle ${h.model.replace(/^forecast\./, '')}` : ''}
                {h.status === 'pending' ? ' · en attente de résolution' : ''}
              </div>
            )}
            {(h.more ?? 0) > 0 && <div className="v8d-big-sub v8-subtle">+ {h.more} autre(s) valeur(s) ci-dessous</div>}
          </div>
        ) : h.kind === 'unknown' ? (
          <div className="v8d-unknown">
            <p className="v8d-unknown-title">REVA ne sait pas.</p>
            {h.detail && <p className="v8d-unknown-detail">{h.detail}</p>}
            {bundle.missing.length > 0 && (
              <ul className="v8d-missing" aria-label="Ce qui manque">
                {bundle.missing.map((m) => (
                  <li key={m}>
                    <span className="v8d-hatch" aria-hidden="true" /> manquant : {m}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p className="v8d-sentence-head">
            {h.text}
            {h.count ? <span className="v8-subtle"> ({h.count})</span> : null}
          </p>
        )}
      </div>

      <div className="v8d-actions" role="toolbar" aria-label="Actions sur la réponse">
        {onPin && (
          <button
            type="button"
            className="v8-btn v8-btn-sm"
            onClick={async () => {
              setPinMsg('…');
              try {
                setPinMsg((await onPin()) ?? 'Épinglé');
              } catch (e) {
                setPinMsg(e instanceof Error ? e.message : 'Échec de l’épinglage');
              }
            }}
          >
            Épingler
          </button>
        )}
        <button
          type="button"
          className="v8-btn v8-btn-sm v8-btn-ghost"
          onClick={() => {
            try {
              void navigator.clipboard?.writeText(window.location.origin + v8Links.ask(bundle.question));
              setCopied(true);
            } catch {
              setCopied(false);
            }
          }}
        >
          {copied ? 'Lien copié' : 'Copier le lien'}
        </button>
        {bundle.targets.map((t, i) => {
          const href = targetHref(t);
          return href ? (
            <Link key={`${t.kind}-${i}`} className="v8-btn v8-btn-sm v8-btn-ghost" to={href} title={t.label ?? undefined}>
              {TARGET_LABEL[t.kind]}
              <span className="v8d-target-label"> · {(t.label ?? '').slice(0, 28)}</span>
            </Link>
          ) : null;
        })}
        <Link className="v8-btn v8-btn-sm v8-btn-ghost" to={brainstormHref(bundle.question)}>
          Brainstormer
        </Link>
        {pinMsg && <span className="v8-subtle v8d-pinmsg">{pinMsg}</span>}
      </div>

      <ol className="v8d-sentences">
        {body.map((s, i) => (
          <SentenceRow key={s.id} s={s} i={i + 1} onCite={cite} />
        ))}
      </ol>

      {bundle.viz && (
        <section className="v8d-section v8d-reveal" style={{ ['--i' as string]: 3 }} aria-label="Preuves">
          <EvidenceView viz={bundle.viz as Record<string, unknown>} onCite={cite} />
        </section>
      )}

      <section className="v8d-section" aria-label="Sources et licences">
        <h3 className="v8d-h3">Sources et licences</h3>
        <ul className="v8d-sources">
          {bundle.sources.map((s) => (
            <li key={s.source}>
              <SourceChip source={s.source} licence={s.licence ? { licence: s.licence, non_commercial: s.non_commercial, restricted: s.restricted } : null} details={[`${s.n} preuve(s)`, ...s.kinds.map(evidenceKindFr), ...(s.tiers.length ? [`niveaux ${s.tiers.join(', ')}`] : [])]} citation={s.community ? { label: s.source, source: s.source, href: null, revision: null, revisionHref: null, community: true, fixture: false, details: [] } : null} />
              <span className="v8-subtle v8d-src-lic">{s.licence ?? 'licence non renseignée'}</span>
              {s.non_commercial && <Badge kind="licence">non commercial</Badge>}
              {s.outside_snapshot && <Badge kind="info">hors instantané</Badge>}
            </li>
          ))}
          {bundle.sources.length === 0 && <li className="v8-subtle">Aucune source citée : la réponse ne contient que des absences.</li>}
        </ul>
        {sourcesSentence && <p className="v8-subtle v8d-src-sentence">{sourcesSentence.text}</p>}
      </section>

      {bundle.followups.length > 0 && (
        <section className="v8d-section" aria-label="Pour aller plus loin">
          <h3 className="v8d-h3">Pour aller plus loin</h3>
          <div className="v8d-followups">
            {bundle.followups.map((f) =>
              f.q ? (
                <button key={f.label} type="button" className="v8d-chip" onClick={() => onAsk(f.q as string)}>
                  {f.label}
                </button>
              ) : f.brainstorm !== null ? (
                <Link key={f.label} className="v8d-chip" to={brainstormHref(f.brainstorm || bundle.question)}>
                  {f.label}
                </Link>
              ) : f.target && targetHref(f.target) ? (
                <Link key={f.label} className="v8d-chip" to={targetHref(f.target) as string}>
                  {f.label}
                </Link>
              ) : null,
            )}
          </div>
        </section>
      )}

      <details className="v8d-section v8d-how">
        <summary className="v8d-h3">Comment c’est calculé</summary>
        <HowView bundle={bundle} />
      </details>

      <details className="v8d-section" open={focus !== null}>
        <summary className="v8d-h3">Preuves ({bundle.evidence.length})</summary>
        <ul className="v8d-evlist">
          {bundle.evidence.map((e) => (
            <EvidenceRow key={e.id} e={e} focused={focus === e.id} />
          ))}
        </ul>
      </details>
    </article>
  );
}

function SentenceRow({ s, i, onCite }: { s: Sentence; i: number; onCite: (id: string) => void }) {
  const tag = SENTENCE_KIND[s.kind];
  return (
    <li className={`v8d-s v8d-s-${s.kind} v8d-reveal`} style={{ ['--i' as string]: Math.min(i, 12) }}>
      {tag && <span className={`v8d-s-tag v8d-s-tag-${s.kind}`}>{tag}</span>}
      <span className="v8d-s-text">{s.text}</span>
      {s.citations.length > 0 && (
        <span className="v8d-s-cites">
          {s.citations.slice(0, 8).map((c, k) => (
            <button key={c} type="button" className="v8d-eid" title={s.citation_refs[k] ?? c} onClick={() => onCite(c)}>
              {c}
            </button>
          ))}
          {s.citations.length > 8 && <span className="v8-subtle"> +{s.citations.length - 8}</span>}
        </span>
      )}
    </li>
  );
}

function EvidenceRow({ e, focused }: { e: Evidence; focused: boolean }) {
  const src = evidenceSource(e);
  const ex = e as unknown as Record<string, unknown>;
  const v = typeof ex.value === 'string' ? ex.value : null;
  return (
    <li id={`v8d-ev-${e.id}`} className={`v8d-ev${focused ? ' is-focus' : ''}`}>
      <span className="v8d-eid-static">{e.id}</span>
      <span className="v8d-ev-kind">{evidenceKindFr(e.kind)}</span>
      {v !== null && (
        <span className="v8-num">
          {displayValue(v, 4)} {typeof ex.unit === 'string' ? ex.unit : ''}
        </span>
      )}
      {e.kind !== 'absence' && e.kind !== 'registry_term' && e.kind !== 'entity_identification' ? (
        <SourceChip {...src} />
      ) : (
        <span className="v8-subtle v8d-ev-what">{String(ex.what ?? ex.query ?? ex.qid ?? ex.term_id ?? '')}</span>
      )}
      {e.kind === 'absence' && Array.isArray(ex.sources_consulted) && (
        <span className="v8-subtle v8d-ev-consulted">consulté : {(ex.sources_consulted as unknown[]).map(String).join(', ')}</span>
      )}
    </li>
  );
}

function HowView({ bundle }: { bundle: AnswerBundle }) {
  const h = bundle.how;
  const asOf = h.as_of as Record<string, unknown>;
  const snaps = Array.isArray(asOf.atlas_snapshots) ? (asOf.atlas_snapshots as string[]) : [];
  const reg = Array.isArray(asOf.registered_tables) ? (asOf.registered_tables as Record<string, string>[]) : [];
  const seals = Array.isArray(asOf.forecast_seals) ? (asOf.forecast_seals as string[]) : [];
  const kgr = asOf.kg_retrieved as { first?: string; last?: string } | null | undefined;
  return (
    <div className="v8d-how-body">
      <p>
        Planificateur : <strong>{h.planner === 'keyword_fallback' ? 'règles déterministes (sans modèle de langage)' : h.planner}</strong>
        {h.prompt_version ? <span className="v8-subtle"> · {h.prompt_version}</span> : null}
      </p>
      {h.restatement && <p className="v8-subtle">Plan validé : {h.restatement}</p>}
      <table className="v8d-table">
        <caption className="v8-sr-only">Opérations exécutées</caption>
        <thead>
          <tr>
            <th scope="col">Opération</th>
            <th scope="col">Arguments</th>
          </tr>
        </thead>
        <tbody>
          {h.operations.map((o, i) => (
            <tr key={`${o.op}-${i}`}>
              <td>
                <code>{o.op}</code>
              </td>
              <td className="v8d-args">
                {Object.entries(o.args)
                  .map(([k, v]) => `${k} = ${Array.isArray(v) ? v.join(', ') : String(v)}`)
                  .join(' · ')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {h.rejected.length > 0 && <p className="v8d-warn">{h.rejected.length} opération(s) refusée(s) par la validation du plan.</p>}
      {h.tables.length > 0 && (
        <p>
          Lu dans : {h.tables.map((t) => `${t.table} (${t.n})`).join(' ; ')}
        </p>
      )}
      {h.periods && (
        <p>
          Périodes : {h.periods.first} → {h.periods.last}
        </p>
      )}
      <p className="v8-subtle">
        État des données :{' '}
        {[
          snaps.length ? `instantané atlas ${snaps.map((x) => x.slice(0, 12)).join(', ')}…` : null,
          ...reg.map((r) => `table ${r.dataset_key} enregistrée le ${(r.registered_at ?? '').slice(0, 10)} (hors instantané)`),
          seals.length ? `prévisions scellées le ${seals.map((x) => x.slice(0, 10)).join(', ')}` : null,
          kgr?.first ? `graphe récupéré ${kgr.first.slice(0, 10)} → ${(kgr.last ?? '').slice(0, 10)}` : null,
        ]
          .filter(Boolean)
          .join(' · ') || 'non daté'}
      </p>
      {h.warnings.length > 0 && <p className="v8-subtle">Avertissements : {h.warnings.join(' ; ')}</p>}
      <p className="v8-subtle">
        Chaque phrase est composée par des gabarits à partir des preuves citées ; aucun nombre n’est calculé ni moyenné ; une absence est une absence de donnée chargée, pas une absence dans le monde.
      </p>
    </div>
  );
}
