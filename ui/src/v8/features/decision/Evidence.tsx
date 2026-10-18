import { Badge } from '../../shell/ui';
import type { Conflict, Evidence as Ev, GraphEdge, RunBundle } from './api';
import { num } from './model';

const METHOD_FR: Record<string, string> = {
  local_projection: 'projections locales',
  svar: 'VAR structurel',
  bvar: 'VAR bayésien',
  var: 'VAR',
  meta_analysis: 'méta-analyse',
  structural_model: 'modèle structurel',
  dsge: 'DSGE',
  event_study: 'étude d’événement',
  panel_regression: 'régression de panel',
  accounting_identity: 'identité comptable',
};

const APPLIC_FR: Record<string, string> = {
  exact: 'portée : ce pays exactement',
  panel_member: 'panel groupé (le pays est dans l’échantillon)',
  world_panel: 'panel mondial transposé',
  group_unverified: 'groupe de pays (appartenance non vérifiée)',
  scope_unstated: 'portée non précisée par la source',
};

export function Citation({ ev }: { ev: Ev }) {
  return (
    <div className="v8d-cite">
      <p className="v8d-cite-src">
        {ev.citation ?? ev.short}
        {ev.restricted && (
          <Badge kind="licence" title={ev.rights?.licence ?? undefined}>
            usage restreint
          </Badge>
        )}
        {ev.non_commercial && <Badge kind="licence">non commercial</Badge>}
        {ev.source_kind === 'reva_run' && <Badge kind="estimate">calcul REVA</Badge>}
      </p>
      {ev.quote && (
        <blockquote className="v8d-quote" cite={ev.url ?? undefined}>
          « {ev.quote} »
        </blockquote>
      )}
      <dl className="v8d-facts">
        {ev.locator && (
          <>
            <dt>Emplacement</dt>
            <dd>{ev.locator}</dd>
          </>
        )}
        {(ev.doi || ev.url) && (
          <>
            <dt>Lien</dt>
            <dd>
              {ev.doi && (
                <a href={`https://doi.org/${ev.doi}`} target="_blank" rel="noreferrer noopener">
                  doi:{ev.doi}
                </a>
              )}
              {ev.doi && ev.url && ' · '}
              {ev.url && (
                <a href={ev.url} target="_blank" rel="noreferrer noopener">
                  source
                </a>
              )}
            </dd>
          </>
        )}
        {ev.run?.run_id && (
          <>
            <dt>Calcul</dt>
            <dd className="v8-mono">
              run {ev.run.run_id} · données au {ev.run.as_of} · code {String(ev.run.code_version ?? '').slice(0, 10)}
            </dd>
          </>
        )}
        {ev.method && (
          <>
            <dt>Méthode</dt>
            <dd>
              {METHOD_FR[ev.method] ?? ev.method}
              {ev.identification && <span className="v8-muted"> — {ev.identification}</span>}
            </dd>
          </>
        )}
        {ev.rights?.licence && (
          <>
            <dt>Licence</dt>
            <dd>{ev.rights.licence}</dd>
          </>
        )}
        {ev.as_of != null && (
          <>
            <dt>Connue au</dt>
            <dd className="v8-num">{String(ev.as_of)}</dd>
          </>
        )}
        {ev.curation_review && (
          <>
            <dt>Relecture</dt>
            <dd>{ev.curation_review === 'maintainer_review_pending' ? 'relecture humaine en attente' : ev.curation_review}</dd>
          </>
        )}
        <dt>Clé</dt>
        <dd className="v8-mono v8d-key">{ev.estimate_key}</dd>
      </dl>
      {ev.applicability_note && <p className="v8d-note">{ev.applicability_note}</p>}
      {ev.wording_fr && <p className="v8d-wording">{ev.wording_fr}</p>}
    </div>
  );
}

export function ConflictTable({ c, evidence }: { c: Conflict; evidence: RunBundle['evidence'] }) {
  return (
    <section className="v8d-conflict" aria-label={c.title_fr}>
      <h4>{c.title_fr}</h4>
      {c.message_fr && <p className="v8-muted v8d-small">{c.message_fr}</p>}
      <div className="v8d-conflict-grid">
        {c.estimates.map((x) => {
          const ev = evidence[x.estimate_key];
          return (
            <article key={x.estimate_key} className={`v8d-conflict-card${x.chosen ? ' is-chosen' : ''}`}>
              <header>
                <strong>{x.short}</strong> {x.chosen ? <Badge kind="estimate">retenue</Badge> : <Badge kind="neutral">{c.kind === 'tie' ? 'à égalité' : 'non retenue'}</Badge>}
              </header>
              <p className="v8-num v8d-pub">{x.published_fr ?? (x.point_used != null ? num(x.point_used, true) : '—')}</p>
              <p className="v8-muted v8d-small">{x.published_uncertainty_fr}</p>
              {x.applicability && <p className="v8d-small">{APPLIC_FR[x.applicability] ?? x.applicability}</p>}
              {x.method && <p className="v8d-small">{METHOD_FR[x.method] ?? x.method}</p>}
              {ev?.quote && <blockquote className="v8d-quote v8d-quote-sm">« {ev.quote} »</blockquote>}
              {ev?.locator && <p className="v8-muted v8d-small">{ev.locator}</p>}
            </article>
          );
        })}
      </div>
      <p className="v8-muted v8d-small">Côte à côte, jamais additionnées ni moyennées. {c.kind === 'alternatives' ? 'Règle de choix déterministe du moteur (portée, incertitude publiée, profil daté, méthode, préférence déclarée, date).' : 'Aucune n’est retenue : le moteur ne tranche pas seul.'}</p>
    </section>
  );
}

export function EdgeEvidence({ edge, bundle }: { edge: GraphEdge; bundle: RunBundle }) {
  const ev = edge.estimate_key ? bundle.evidence[edge.estimate_key] : undefined;
  const conflicts = bundle.conflicts.filter((c) => c.edges.includes(edge.id) || (edge.kind === 'tie' && c.kind === 'tie' && c.nodes.includes(edge.to)));
  const from = bundle.graph.nodes.find((n) => n.id === edge.from)?.label ?? edge.from;
  const to = bundle.graph.nodes.find((n) => n.id === edge.to)?.label ?? edge.to;
  return (
    <div className="v8d-evidence" aria-live="polite">
      <p className="v8d-edge-title">
        {from} <span aria-hidden="true">→</span> {to}
      </p>
      {edge.kind === 'assumption' && <p>Hypothèse de scénario énoncée (pas une estimation) : {edge.basis_fr}</p>}
      {edge.kind === 'plugin' && <p>Modèle REVA sous hypothèses énoncées : {edge.assumption_fr}</p>}
      {edge.kind === 'missing' && (
        <p>
          <Badge kind="missing">lien manquant</Badge> {(bundle.graph.nodes.find((n) => n.id === edge.to)?.reasons ?? []).join(' ; ') || 'aucune estimation chiffrée ne part de cette variable'}
        </p>
      )}
      {edge.kind === 'sign_only' && <p>Estimation de signe seulement : aucun nombre n’est propagé.</p>}
      {edge.kind === 'estimate' && edge.effect && (
        <>
          <dl className="v8d-facts v8d-effect">
            <dt>Estimation publiée</dt>
            <dd className="v8-num">{edge.effect.published_fr ?? '—'}</dd>
            <dt>Incertitude</dt>
            <dd>{edge.effect.published_uncertainty_fr}</dd>
            <dt>Coefficient appliqué</dt>
            <dd className="v8-num">
              {edge.effect.beta_label_fr}
              {edge.effect.beta_ci90 ? ` · 90 % [${num(edge.effect.beta_ci90[0], true)} ; ${num(edge.effect.beta_ci90[1], true)}] (approx. normale)` : ' · sans incertitude publiée'}
            </dd>
            {edge.applicability && (
              <>
                <dt>Applicabilité</dt>
                <dd>{APPLIC_FR[edge.applicability.status] ?? edge.applicability.status}</dd>
              </>
            )}
          </dl>
          {[...(edge.applicability_notes_fr ?? []), ...(edge.notes_fr ?? [])].length > 0 && (
            <ul className="v8d-notes">
              {[...(edge.applicability_notes_fr ?? []), ...(edge.notes_fr ?? [])].map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
          {ev && <Citation ev={ev} />}
        </>
      )}
      {conflicts.map((c, i) => (
        <ConflictTable key={i} c={c} evidence={bundle.evidence} />
      ))}
    </div>
  );
}
