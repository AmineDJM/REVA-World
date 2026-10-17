import { Link } from 'react-router';
import { v8Links } from '../../contract';
import { Badge, MissingValue } from '../../shell/ui';
import type { BrainstormBundle, Driver, SeriesStatus } from './api';

function SeriesLine({ s }: { s: SeriesStatus }) {
  if (s.status === 'chargé') {
    return (
      <span className="v8b-series">
        <span className="v8b-ok" aria-hidden="true" />
        <code>{s.concept}</code> · dernière période <strong>{s.last_period}</strong> · {s.source}
        {s.stale ? (
          <Badge kind="neutral" title="Dernière valeur chargée de plus de deux ans.">
            ancienne
          </Badge>
        ) : null}
      </span>
    );
  }
  return (
    <span className="v8b-series">
      <MissingValue reason={s.status === 'manquant' ? `aucune valeur chargée pour ${s.concept}` : `${s.concept} : ${s.status}`} compact />
      <code className="v8-subtle"> {s.concept}</code>
    </span>
  );
}

function DriverRow({ d }: { d: Driver }) {
  const signs = Object.entries(d.signs)
    .map(([k, n]) => `${n} ${k === 'positive' ? 'positive(s)' : k === 'negative' ? 'négative(s)' : k}`)
    .join(', ');
  const loaded = d.data.series.some((s) => s.status === 'chargé');
  return (
    <li className={`v8b-driver${loaded ? '' : ' is-missing'}`}>
      <div className="v8b-driver-head">
        <strong>{d.label}</strong> <code className="v8-subtle">{d.id}</code>
        <Badge kind="estimate" title="Estimations publiées attribuées à leurs auteurs ; jamais moyennées.">
          {d.n} estimation(s)
        </Badge>
        {d.covers_country && <Badge kind="info">couvre le pays</Badge>}
      </div>
      <div className="v8b-driver-meta v8-subtle">
        sur {d.effects.join(', ')} · signes : {signs || 'non publiés'} · {Object.keys(d.methods).join(', ')}
      </div>
      {d.references.length > 0 && <div className="v8b-refs">Selon {d.references.join(' ; ')}</div>}
      <div className="v8b-data">
        {d.data.series.length > 0 ? (
          d.data.series.map((s) => <SeriesLine key={s.concept} s={s} />)
        ) : (
          <MissingValue reason="aucune série de l’atlas reliée à cette variable par un curateur" />
        )}
        {d.data.link && <span className="v8-subtle v8b-link"> ({d.data.link})</span>}
      </div>
      {d.data.candidates.length > 0 && (
        <div className="v8b-cands v8-subtle">
          Candidats du catalogue (par les mots du libellé, non reliés par un curateur) :{' '}
          {d.data.candidates.map((c) => (
            <code key={c.concept} title={c.label ?? ''}>
              {c.concept}
            </code>
          ))}
        </div>
      )}
      {d.data.queued.length > 0 && (
        <ul className="v8b-queued">
          {d.data.queued.map((q) => (
            <li key={`${q.file}:${q.line}`} className="v8-subtle">
              <code>
                {q.file}:{q.line}
              </code>{' '}
              — {q.text.slice(0, 220)}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function BrainstormView({ b }: { b: BrainstormBundle }) {
  const t = b.target;
  return (
    <article className="v8b" data-testid="v8b-result">
      <header className="v8d-answer-head">
        <Badge kind={b.known ? 'info' : 'missing'}>{b.known ? 'Cible reconnue dans les registres' : 'Cible inconnue des registres'}</Badge>
        <span className="v8-subtle v8d-meta">
          « {t.text} »{t.iso3 ? ` · ${t.iso3}` : ''}
          {t.horizon ? ` · horizon ${t.horizon.n} ${t.horizon.unit}` : ''}
          {b.latency_ms ? ` · ${Math.round(b.latency_ms)} ms` : ''}
        </span>
      </header>

      <section className="v8d-section">
        <h3 className="v8d-h3">Prochaines étapes</h3>
        {b.next_steps.length ? (
          <ol className="v8b-steps">
            {b.next_steps.map((s) => (
              <li key={s.rank}>
                <Badge kind="info">{s.kind}</Badge> {s.text}
              </li>
            ))}
          </ol>
        ) : (
          <p className="v8-subtle">Aucune étape ne ressort des règles.</p>
        )}
        <p className="v8-subtle v8b-note">Suggestions de REVA (règles explicites), pas une promesse de gain de précision.</p>
      </section>

      <section className="v8d-section">
        <h3 className="v8d-h3">Cibles reconnues</h3>
        <div className="v8b-anchors">
          {b.anchors.families.map((f) => (
            <span key={f.key} className="v8d-chip is-static" title="famille de questions scellées">
              prévision · {f.label ?? f.key}
            </span>
          ))}
          {b.anchors.concepts.map((c) => (
            <span key={c.id} className="v8d-chip is-static" title={c.id}>
              série · {c.label ?? c.id}
            </span>
          ))}
          {b.anchors.scoreboard.map((c) => (
            <Link key={c.id} className="v8d-chip" to={v8Links.scoreboard(c.id)}>
              scores · {c.label ?? c.id}
            </Link>
          ))}
          {b.anchors.effect_variables.map((v) => (
            <span key={v.id} className="v8d-chip is-static" title={v.id}>
              variable · {v.labels.fr ?? v.labels.en ?? v.id}
              {v.match && v.match !== 'libellé' ? ` (${v.match})` : ''}
            </span>
          ))}
          {!b.known && <MissingValue reason="aucun terme des registres de REVA ne correspond à ces mots" />}
        </div>
        {b.target_series.length > 0 && (
          <div className="v8b-target-series">
            {b.target_series.map((s) => (
              <SeriesLine key={s.concept} s={s} />
            ))}
          </div>
        )}
      </section>

      <section className="v8d-section">
        <h3 className="v8d-h3">Pilotes attribués ({b.drivers.length})</h3>
        {b.drivers.length ? (
          <ul className="v8b-drivers">
            {b.drivers.map((d) => (
              <DriverRow key={d.id} d={d} />
            ))}
          </ul>
        ) : (
          <MissingValue reason="aucune estimation publiée chargée sur cette cible dans la couche d’effets" block />
        )}
        {b.claims.length > 0 && (
          <ul className="v8b-claims">
            {b.claims.map((c, i) => {
              const cause = (c.cause ?? {}) as Record<string, unknown>;
              const eff = (c.effect ?? {}) as Record<string, unknown>;
              return (
                <li key={i} className="v8-subtle">
                  Selon {String(c.source)} ({String(c.tier)}) : {String(cause.label ?? cause.id)} → {String(eff.label ?? eff.id)}
                  {c.grade ? ` · preuve ${String(c.grade)}` : ''}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="v8d-section">
        <h3 className="v8d-h3">Prévisions existantes et jugement</h3>
        {b.forecasts.length ? (
          <ul className="v8b-fc">
            {b.forecasts.map((f) => (
              <li key={f.family}>
                <strong>{f.label ?? f.family}</strong> <code className="v8-subtle">{f.family}</code>
                {f.covers ? (
                  <span className="v8-subtle">
                    {' '}
                    · {f.n_questions} question(s) scellée(s)
                    {f.periods ? ` (${f.periods.first} → ${f.periods.last})` : ''} ·{' '}
                    {Object.entries(f.statuses)
                      .map(([k, n]) => `${n} ${k === 'pending' ? 'en attente' : k === 'resolved' ? 'résolue(s)' : k}`)
                      .join(', ')}
                  </span>
                ) : (
                  <MissingValue reason={`famille sans question pour ${b.target.iso3 ?? 'ce pays'}`} compact />
                )}
                {f.models.length > 0 && (
                  <div className="v8-subtle v8b-models">
                    Modèles : {f.models.map((m) => `${m.model.replace(/^forecast\./, '')}${m.method ? ` (${m.method})` : ''}`).join(' ; ')}
                  </div>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <MissingValue reason="aucune famille de prévisions scellées ne correspond" />
        )}
        {b.verdicts.map((v) => (
          <div key={v.target} className="v8b-verdict">
            <strong>{v.label}</strong>{' '}
            {Object.entries(v.verdicts).map(([k, n]) => (
              <Badge key={k} kind={k === 'bat' ? 'win' : k === 'ne bat pas' ? 'loss' : 'neutral'}>
                {n} × {k}
              </Badge>
            ))}{' '}
            <Link to={v8Links.scoreboard(v.target)}>tableau des scores →</Link>
            <ul className="v8b-examples">
              {v.examples.map((e, i) => (
                <li key={i} className="v8-subtle">
                  {String(e.candidate ?? '')} vs {String(e.benchmark ?? '')} : {String(e.verdict ?? '')}
                  {e.ratio !== null && e.ratio !== undefined ? ` (ratio ${String(e.ratio)})` : ''} — {String(e.basis ?? '')}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </section>

      <p className="v8-subtle v8b-method">{b.method}</p>
    </article>
  );
}
