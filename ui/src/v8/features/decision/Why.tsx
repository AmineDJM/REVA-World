import { Badge, EmptyState } from '../../shell/ui';
import type { DiagItem, RunBundle } from './api';

function Item({ it }: { it: DiagItem }) {
  return (
    <li className="v8d-why-item">
      <p className="v8d-why-title">
        <Badge kind="missing">{it.title_fr}</Badge> {it.node_label ?? it.plugin ?? ''}
      </p>
      <p>{it.detail_fr}</p>
      {it.missing_fr && (
        <p>
          <strong>Il manque :</strong> {it.missing_fr}
        </p>
      )}
      {it.missing && it.missing.length > 0 && (
        <div>
          <strong>Il manque :</strong>
          <ul className="v8d-mono-list">
            {it.missing.map((m) => (
              <li key={m} className="v8-mono">
                {m}
              </li>
            ))}
          </ul>
        </div>
      )}
      {it.estimate_keys && it.estimate_keys.length > 0 && <p className="v8-mono v8d-small">{it.estimate_keys.join(' · ')}</p>}
      {it.unblock_fr && (
        <p className="v8d-unblock">
          <strong>Pour débloquer :</strong> {it.unblock_fr}
        </p>
      )}
      {it.near && it.near.length > 0 && (
        <details>
          <summary>{it.near.length} estimation(s) voisine(s), non applicables telles quelles</summary>
          <ul>
            {it.near.map((n) => (
              <li key={n.estimate_key ?? n.cause ?? ''}>
                <span className="v8-mono">{n.cause}</span> → <span className="v8-mono">{n.effect}</span>
                {n.wording_fr && <p className="v8-muted v8d-small">{n.wording_fr}</p>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}

/** « Pourquoi pas de résultat » : exactly which link or data is missing, where each chain stops. */
export function Why({ bundle }: { bundle: RunBundle }) {
  const d = bundle.diagnosis;
  const r = bundle.result;
  const blocking = [...d.blocking, ...d.plugins, ...d.ties, ...d.other];
  const excludedByCode = r.excluded_links.reduce<Record<string, number>>((acc, e) => ({ ...acc, [e.code]: (acc[e.code] ?? 0) + 1 }), {});
  return (
    <div className="v8d-why">
      <p className="v8d-answer">{d.headline_fr}</p>
      {blocking.length > 0 ? (
        <ol className="v8d-why-list">
          {blocking.map((it, i) => (
            <Item key={i} it={it} />
          ))}
        </ol>
      ) : (
        d.has_result && <p className="v8-muted">Aucun lien bloquant : la chaîne s’arrête seulement là où aucune estimation ne part des variables atteintes.</p>
      )}
      {d.missing_periods.length > 0 && (
        <section>
          <h4>Périodes non calculées</h4>
          <ul>
            {d.missing_periods.map((m) => (
              <li key={m.variable}>
                {m.label} ({m.geo}) : {m.periods.join(', ')} — {m.reason_fr}
              </li>
            ))}
          </ul>
        </section>
      )}
      {d.chain_ends.length > 0 && (
        <p className="v8d-small">
          <strong>Fin de chaîne</strong> (aucune estimation ne part de ces variables) : {d.chain_ends.map((c) => `${c.label}`).join(' · ')}
        </p>
      )}
      {r.unquantified_links.length > 0 && (
        <section>
          <h4>Liens non chiffrés ({r.unquantified_links.length}) — aucun nombre</h4>
          <ul className="v8d-unq">
            {r.unquantified_links.map((u, i) => (
              <li key={i}>
                {u.wording_fr ?? u.note_fr} {u.grade && <Badge kind="community">grade {u.grade}</Badge>}
              </li>
            ))}
          </ul>
        </section>
      )}
      {r.excluded_links.length > 0 && (
        <details>
          <summary>
            {r.excluded_links.length} estimation(s) écartée(s) : {Object.entries(excludedByCode).map(([k, n]) => `${n} ${k === 'out_of_scope' ? 'hors champ' : k === 'not_propagatable' ? 'non propageables' : k === 'unit_mismatch' ? 'unité incompatible' : k}`).join(', ')}
          </summary>
          <ul className="v8d-excluded">
            {r.excluded_links.slice(0, 200).map((e, i) => (
              <li key={i}>
                <span className="v8-mono">{e.estimate_key}</span> {e.geo ? `(${e.geo})` : ''} — {e.detail}
              </li>
            ))}
          </ul>
        </details>
      )}
      {!blocking.length && !d.missing_periods.length && !r.unquantified_links.length && !d.has_result && <EmptyState title="Aucune explication">Le moteur n’a signalé aucun arrêt.</EmptyState>}
    </div>
  );
}
