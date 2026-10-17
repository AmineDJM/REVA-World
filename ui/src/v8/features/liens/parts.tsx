import { Link } from 'react-router';
import { SourceChip } from '../../shell/ui';
import { v8Links } from '../../contract';
import { kindLabel } from '../../../v3/lib/kinds';
import type { EgoData, Hop, NodeRef, PathOut, PathsData } from './api';
import { KIND_FR, PATH_KIND_FR, hopTone, licenceShort, nodeLabel, plural, strengthShort, toRights } from './model';

const TONE_TITLE: Record<string, string> = {
  documented: 'Lien publié par une source (déclaration de jeu de données) ; REVA ne le vérifie pas.',
  community: 'Déclaration Wikidata : contribution communautaire, non vérifiée.',
  claim: 'Affirmation causale attribuée à sa source, avec palier (T1–T4) et note (A–E) ; REVA n’établit pas la causalité.',
  estimate: 'Estimation d’effet publiée par ses auteurs (méthode, incertitude) ; pas une observation.',
  declared: 'Lien d’identifiant déclaré dans un registre (périmètre d’une estimation, concept atlas, classe Wikidata vérifiée).',
  missing: 'Le détail de ce lien n’a pas été retrouvé dans la base (index plus récent ?).',
};

function NodeName({ id, nodes, onPick, showId = false }: { id: string; nodes: Record<string, NodeRef>; onPick: (id: string) => void; showId?: boolean }) {
  const n = nodes[id];
  return (
    <button type="button" className={`lien-node-btn${n?.virtual ? ' is-virtual' : ''}${n && !n.label_loaded ? ' is-unlabelled' : ''}`} onClick={() => onPick(id)} title={`${id}${n?.kind ? ` · ${n.kind}` : ''}`}>
      {nodeLabel(nodes, id)}
      {showId && <span className="lien-node-id v8-mono"> {id}</span>}
    </button>
  );
}

export function HopRow({ h, nodes, onPick }: { h: Hop; nodes: Record<string, NodeRef>; onPick: (id: string) => void }) {
  const tone = hopTone(h);
  const lic = licenceShort(h);
  // two records with the same name (a protein and the drug target built on it): show their ids
  const same = nodeLabel(nodes, h.from) === nodeLabel(nodes, h.to);
  const detail = h.detail ?? {};
  const wording = typeof detail.wording_fr === 'string' ? detail.wording_fr : null;
  const value = typeof detail.value === 'string' ? detail.value : null;
  const details: string[] = [];
  if (h.source?.statement_id) details.push(`Déclaration : ${h.source.statement_id}`);
  if (h.source?.revision) details.push(`Révision : ${h.source.revision}`);
  if (h.property) details.push(`Propriété de la source : ${h.property.pid}${h.property.label_fr ? ` (${h.property.label_fr})` : ''}`);
  if (h.family) details.push(`Famille de relation : ${h.family.id} (jamais causale)`);
  if (h.origin === 'test_fixture') details.push('Donnée de test (test_fixture)');
  return (
    <li className={`lien-hop tone-${tone}`} data-testid="lien-hop">
      <span className="lien-hop-rail" aria-hidden="true" />
      <div className="lien-hop-main">
        <p className="lien-hop-sentence">
          <NodeName id={h.from} nodes={nodes} onPick={onPick} showId={same} />
          <span className="lien-rel">
            {' — '}
            {h.relation?.fr ?? KIND_FR[h.kind]}
            {' → '}
          </span>
          <NodeName id={h.to} nodes={nodes} onPick={onPick} showId={same} />
        </p>
        <p className="lien-hop-meta">
          <span className={`lien-tag tag-${tone}`} title={TONE_TITLE[tone]}>
            {KIND_FR[h.kind]} · {strengthShort(h)}
          </span>
          {h.source ? (
            <SourceChip
              source={h.source.label}
              label={h.source.citation ?? (h.source.label.length > 34 ? `${h.source.label.slice(0, 32)}…` : h.source.label)}
              licence={toRights(h.licence)}
              retrievedAt={h.source.retrieved_at ?? null}
              details={details}
            />
          ) : (
            <span className="lien-tag tag-missing">source introuvable</span>
          )}
          {!h.licence && <span className={`lien-lic${lic.unknown ? ' is-unknown' : ''}`}>{lic.text}</span>}
          {h.dated && (h.dated.from || h.dated.to) && (
            <span className="lien-date v8-num">
              {h.dated.from ?? '…'} – {h.dated.to ?? '…'}
            </span>
          )}
          {h.alternatives > 0 && <span className="lien-alt">+{plural(h.alternatives, 'autre lien', 'autres liens')} entre ces deux nœuds</span>}
        </p>
        {(wording || value) && (
          <details className="lien-hop-more">
            <summary>{h.kind === 'effect_estimate' ? `valeur publiée : ${value ?? 'non chiffrée'}` : 'formulation attribuée'}</summary>
            {wording && <p>{wording}</p>}
            {h.strength?.reasons && h.strength.reasons.length > 0 && (
              <ul className="lien-reasons">
                {h.strength.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            )}
          </details>
        )}
      </div>
    </li>
  );
}

export function PathCard({ p, d, selected, onSelect, onPick }: { p: PathOut; d: PathsData; selected: boolean; onSelect: () => void; onPick: (id: string) => void }) {
  return (
    <article className={`lien-path${selected ? ' is-selected' : ''}`} aria-current={selected ? 'true' : undefined} data-testid="lien-path">
      <header className="lien-path-head">
        <button type="button" className="lien-path-title" onClick={onSelect} aria-pressed={selected}>
          <span className="lien-path-rank v8-num">{p.rank}</span>
          <span>
            {plural(p.length, 'lien', 'liens')} · {PATH_KIND_FR[p.kind]}
          </span>
        </button>
        <span className="lien-path-badges">
          {p.weakest_grade && (
            <span className="lien-tag tag-claim" title="Note de la chaîne = maillon causal le plus faible (règle des chaînes causales)">
              maillon le plus faible : {p.weakest_grade}
            </span>
          )}
          {p.restricted_licences.length > 0 && (
            <span className="lien-tag tag-licence" title={p.restricted_licences.join(' ; ')}>
              licence restrictive
            </span>
          )}
          {p.unknown_licences > 0 && <span className="lien-tag tag-missing">{plural(p.unknown_licences, 'licence inconnue', 'licences inconnues')}</span>}
        </span>
      </header>
      <ol className="lien-hops">
        {p.hops.map((h, i) => (
          <HopRow key={`${h.from}-${h.to}-${i}`} h={h} nodes={d.nodes} onPick={onPick} />
        ))}
      </ol>
      {p.variants && p.variants.count > 0 && (
        <p className="lien-variants">
          + {plural(p.variants.count, 'variante', 'variantes')} identique{p.variants.count > 1 ? 's' : ''} à un nœud près
          {p.variants.examples.length > 0 && <> (p. ex. {p.variants.examples.map((x) => nodeLabel(d.nodes, x)).join(', ')})</>}
        </p>
      )}
    </article>
  );
}

export function NodeCard({ node, onExpand, onFrom, onTo, expanded, busy }: { node: NodeRef; onExpand: () => void; onFrom: () => void; onTo: () => void; expanded: boolean; busy: boolean }) {
  const fiche = node.fiche ? v8Links.entity(node.fiche.kind, node.fiche.id) : null;
  return (
    <div className="lien-nodecard" data-testid="lien-nodecard">
      <p className="lien-nodecard-title">{node.label}</p>
      <p className="lien-nodecard-meta">
        {node.kind_label ?? (node.kind ? kindLabel(node.kind) : 'type non renseigné')} · <span className="v8-mono">{node.id}</span>
        {node.degree !== null && <> · {plural(node.degree, 'lien indexé', 'liens indexés')}</>}
      </p>
      {node.virtual && <p className="lien-nodecard-note">Nœud d’estimation (variable d’effet, indicateur ou type d’événement) : pas une entité du graphe de connaissances.</p>}
      <div className="lien-nodecard-actions">
        <button type="button" className="v8-btn v8-btn-sm" onClick={onExpand} disabled={expanded || busy}>
          {expanded ? 'voisins affichés' : busy ? 'chargement…' : 'Développer'} <kbd>e</kbd>
        </button>
        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={onFrom}>
          Partir d’ici
        </button>
        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={onTo}>
          Aller ici
        </button>
        {fiche && (
          <Link className="v8-btn v8-btn-sm v8-btn-primary" to={fiche}>
            Fiche →
          </Link>
        )}
      </div>
    </div>
  );
}

export function EgoList({ data, onPick }: { data: EgoData; onPick: (id: string) => void }) {
  const groups = Object.entries(data.groups).sort((a, b) => b[1] - a[1]);
  return (
    <div className="lien-ego" data-testid="lien-ego">
      <p className="lien-ego-sum">
        {groups.map(([g, n]) => (
          <span key={g} className="lien-chip is-static">
            {g} <span className="v8-num">{n.toLocaleString('fr-FR')}</span>
          </span>
        ))}
      </p>
      <ol className="lien-hops">
        {data.edges.map((e, i) => (
          <HopRow key={`${e.hop.to}-${i}`} h={e.hop} nodes={data.nodes} onPick={onPick} />
        ))}
      </ol>
    </div>
  );
}
