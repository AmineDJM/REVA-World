import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router';
import { getLocations } from '../../api/client';
import { useApi } from '../../lib/useApi';
import { warningsFr } from '../api/common';
import { getPairLinks } from '../api/links';
import { CitationChip } from '../components/CitationChip';
import { FixtureBadge } from '../components/Honesty';
import { V3Empty, V3Error, V3Loading } from '../components/States';
import { iso3OfFeatureKey } from '../lib/choropleth';
import { formatYear } from '../lib/time';
import { buildRelation, ISSUE_LABEL, laneMarks, VOTE_CAVEAT, type KindView, type LinkItem, type RelationView, type Series, type VoteRow } from '../lib/relation';

const ISO3 = /^[A-Z]{3}$/;
const MAX_SERIES_ROWS = 12;

/** "Pays ↔ Pays": every link kind between two countries over time (/v1/links/pair), each link cited. */
export default function RelationPage() {
  const [params, setParams] = useSearchParams();
  const a = (params.get('a') ?? '').toUpperCase();
  const b = (params.get('b') ?? '').toUpperCase();
  const valid = ISO3.test(a) && ISO3.test(b) && a !== b;
  const pair = useApi((s) => getPairLinks(a, b, {}, s), [a, b], valid);
  const locations = useApi((s) => getLocations(undefined, s), []);
  const names = useMemo(() => {
    const m = new Map<string, string>();
    if (locations.status === 'ok')
      for (const f of locations.data.data.features) {
        const iso = iso3OfFeatureKey(f.properties.feature_key);
        if (iso && !m.has(iso)) m.set(iso, f.properties.name ?? iso);
      }
    return m;
  }, [locations.status, locations.data]);
  const view = useMemo(() => (pair.status === 'ok' ? buildRelation(pair.data.data) : null), [pair.status, pair.data]);
  const [draft, setDraft] = useState({ a, b });
  useEffect(() => setDraft({ a, b }), [a, b]);

  const resolve = (v: string): string | null => {
    const t = v.trim();
    if (/^[A-Za-z]{3}$/.test(t)) return t.toUpperCase();
    return [...names.entries()].find(([, n]) => n.toLowerCase() === t.toLowerCase())?.[0] ?? null;
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const ra = resolve(draft.a);
    const rb = resolve(draft.b);
    if (ra && rb) setParams({ a: ra, b: rb });
  };

  return (
    <section className="v3-page v3-relation" aria-labelledby="relation-title">
      <header className="v3-page-head">
        <h1 id="relation-title">
          Pays ↔ Pays{view ? ` — ${view.aLabel} et ${view.bLabel}` : ''}
          {valid && (
            <span className="muted">
              {' '}
              {a} · {b}
            </span>
          )}
        </h1>
        <p className="v3-lede">
          Tous les liens sourcés entre deux pays dans le temps : commerce et migrations dans les deux sens, part de votes identiques à l’ONU, colonisation, frontières,
          langues, traités, conflits, maintien de la paix. Chaque lien cite ses sources.
        </p>
      </header>
      <p className="v3-banner v3-banner-path" role="note" data-testid="link-not-cause">
        <strong>Un lien n’est pas une cause.</strong> Chaque ligne dit seulement « relié via » une source ; une donnée absente ne donne aucun lien (jamais un zéro).
      </p>
      <form className="v3-inline-form" onSubmit={submit} aria-label="Choisir deux pays">
        <label>
          Pays A
          <input list="v3-rel-countries" value={draft.a} onChange={(e) => setDraft((d) => ({ ...d, a: e.target.value }))} placeholder="nom ou ISO3" />
        </label>
        <label>
          Pays B
          <input list="v3-rel-countries" value={draft.b} onChange={(e) => setDraft((d) => ({ ...d, b: e.target.value }))} placeholder="nom ou ISO3" />
        </label>
        <datalist id="v3-rel-countries">
          {[...names.entries()].map(([iso, n]) => (
            <option key={iso} value={n}>
              {iso}
            </option>
          ))}
        </datalist>
        <button type="submit" className="primary">
          Afficher
        </button>
        {valid && (
          <button type="button" onClick={() => setParams({ a: b, b: a })}>
            Inverser A ⇄ B
          </button>
        )}
      </form>
      {!valid && (
        <V3Empty>
          <p>
            <strong>Choisissez deux pays différents</strong> (codes ISO3, par exemple <Link to="/v3/relation?a=BRA&b=PRT">Brésil ↔ Portugal</Link> ou{' '}
            <Link to="/v3/relation?a=DZA&b=FRA">Algérie ↔ France</Link>).
          </p>
        </V3Empty>
      )}
      {valid && pair.status === 'loading' && <V3Loading what="des liens entre les deux pays" />}
      {valid && pair.status === 'error' && <V3Error error={pair.error} what={`Les liens entre ${a} et ${b}`} onRetry={pair.reload} />}
      {pair.status === 'ok' && view && <RelationBody view={view} origin={pair.data.origin ?? null} warnings={warningsFr(pair.data.warnings)} />}
    </section>
  );
}

function RelationBody({ view, origin, warnings }: { view: RelationView; origin: string | null; warnings: string[] }) {
  const total = view.themes.reduce((n, t) => n + t.kinds.reduce((m, k) => m + k.count, 0), 0);
  return (
    <div data-testid="relation-result">
      <p className="v3-meta">
        {total} lien(s) en {view.themes.reduce((n, t) => n + t.kinds.length, 0)} type(s)
        {view.predecessors.length > 0 && `, ${view.predecessors.length} avec des États prédécesseurs`} · {view.storeLabel}{' '}
        {(origin === 'test_fixture' || origin === 'mixed') && <FixtureBadge />}
      </p>
      {warnings.length > 0 && (
        <ul className="v3-warnings" aria-label="Avertissements du serveur">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {view.truncated.length > 0 && <p className="v3-notice">Lecture bornée par le serveur pour : {view.truncated.join(', ')} — d’autres liens peuvent exister.</p>}
      <p>
        <Link to={`/v3/pays/${view.a}`}>Dossier {view.aLabel}</Link> · <Link to={`/v3/pays/${view.b}`}>Dossier {view.bLabel}</Link> ·{' '}
        <Link to={`/v3/chemin?from=${view.a}&to=${view.b}`}>Chemins de {view.aLabel} à {view.bLabel}</Link>
      </p>
      {view.span && <Overview view={view} span={view.span} />}
      {view.themes.length === 0 && view.predecessors.length === 0 && (
        <V3Empty>
          <p>
            <strong>Aucun lien entre {view.aLabel} et {view.bLabel} dans les données consultées.</strong> Ce n’est pas une absence de relation : seulement qu’aucune source chargée
            n’en rapporte.
          </p>
        </V3Empty>
      )}
      {view.themes.map((t) => (
        <section key={t.id} className="v3-domain" aria-labelledby={`rel-${t.id}`} data-theme={t.id}>
          <h2 id={`rel-${t.id}`}>{t.label}</h2>
          {t.kinds.map((k) => (
            <KindBlock key={k.kind} k={k} />
          ))}
        </section>
      ))}
      {view.predecessors.length > 0 && (
        <section className="v3-domain" aria-labelledby="rel-pred">
          <h2 id="rel-pred">Liens avec des États prédécesseurs</h2>
          <p className="field-hint">Un lien avec un État qui a précédé l’autre pays, montré avec la chaîne de succession (selon la source) qui le relie au pays actuel.</p>
          <ul className="v3-rel-items">
            {view.predecessors.map((p) => (
              <li key={p.key} data-testid="relation-predecessor">
                <p>
                  <span className="muted">
                    {p.country} et un prédécesseur de {p.predecessorOf} ({p.predecessor}) :
                  </span>
                </p>
                <ItemLine it={p.link} />
                <p className="muted">Succession :</p>
                <ol className="v3-rel-chain" data-testid="succession-chain">
                  {p.succession.map((s) => (
                    <li key={s.key}>
                      <ItemLine it={s} />
                    </li>
                  ))}
                </ol>
              </li>
            ))}
          </ul>
        </section>
      )}
      {view.without.length > 0 && (
        <details className="v3-link-status" data-testid="relation-without">
          <summary>Aucun lien dans les données consultées pour {view.without.length} type(s) (≠ zéro)</summary>
          <p className="field-hint">« Aucun lien » veut dire qu’aucune source chargée n’en rapporte entre ces deux pays : ce n’est ni une valeur nulle ni la preuve d’une absence.</p>
          <ul>
            {view.without.map((w) => (
              <li key={w.kind} data-kind={w.kind}>
                {w.label}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function Overview({ view, span }: { view: RelationView; span: [number, number] }) {
  const lanes = view.themes.flatMap((t) => t.kinds).map((k) => ({ k, marks: laneMarks(k, span) }));
  return (
    <figure className="v3-rel-overview" aria-label={`Frise des liens de ${formatYear(span[0])} à ${formatYear(span[1])}`}>
      <figcaption className="muted">
        Frise des types de liens, {formatYear(span[0])} – {formatYear(span[1])} (barres = périodes, points = années ; les liens non datés sont listés plus bas)
      </figcaption>
      <ol className="v3-rel-lanes">
        {lanes.map(({ k, marks }) => (
          <li key={k.kind} className="v3-rel-lane" data-testid="relation-lane" data-kind={k.kind}>
            <span className="v3-rel-lane-label">{k.label}</span>
            <span className="v3-rel-track" aria-hidden="true">
              {marks.map((m, i) => (
                <span key={i} className="v3-rel-mark" style={{ left: `${m.left}%`, width: `${m.width}%` }} title={m.title} />
              ))}
            </span>
            {marks.length === 0 && <span className="v3-undated">non daté dans la source</span>}
          </li>
        ))}
      </ol>
    </figure>
  );
}

function KindBlock({ k }: { k: KindView }) {
  const years = k.firstYear !== null ? `${formatYear(k.firstYear)}${k.lastYear !== null && k.lastYear !== k.firstYear ? ` – ${formatYear(k.lastYear)}` : ''}` : '';
  return (
    <article className="v3-rel-kind" data-testid="relation-kind" data-kind={k.kind} aria-labelledby={`k-${k.kind}`}>
      <h3 id={`k-${k.kind}`}>
        {k.label} <span className="muted">· {k.count} lien(s){years ? ` · ${years}` : ''}</span>
      </h3>
      {k.notes.length > 0 && (
        <ul className="field-hint" data-testid="relation-kind-notes">
          {k.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      {k.series.map((s) => (
        <SeriesTable key={s.key} s={s} />
      ))}
      {k.votes.length > 0 && <VoteTable rows={k.votes} />}
      {k.items.length > 0 && (
        <ul className="v3-rel-items">
          {k.items.slice(0, 100).map((it) => (
            <li key={it.key}>
              <ItemLine it={it} />
            </li>
          ))}
        </ul>
      )}
      {k.items.length > 100 && <p className="muted">… {k.items.length - 100} autre(s) lien(s) de ce type.</p>}
    </article>
  );
}

function ItemLine({ it }: { it: LinkItem }) {
  return (
    <span className="v3-rel-item">
      <span>{it.sentence}</span> <span className={it.undated ? 'v3-undated' : 'muted'}>({it.dates || 'dates non précisées'})</span>
      {it.value && <span> · {it.value}</span>}{' '}
      {it.citations.length ? it.citations.slice(0, 4).map((c, j) => <CitationChip key={j} c={c} compact />) : <span className="v3-missing">aucune citation fournie</span>}
      {it.citations.length > 4 && <span className="muted"> +{it.citations.length - 4} preuve(s)</span>}
    </span>
  );
}

function SeriesTable({ s }: { s: Series }) {
  const rows = s.points;
  const head = rows.length > MAX_SERIES_ROWS ? [...rows.slice(0, 3), null, ...rows.slice(-(MAX_SERIES_ROWS - 4))] : rows;
  const body = (list: (Series['points'][number] | null)[]) =>
    list.map((p, i) =>
      p ? (
        <tr key={`${p.year}-${i}`} data-year={p.year}>
          <th scope="row">{formatYear(p.year)}</th>
          <td className="v3-num" title={p.value}>
            {p.shown}
          </td>
          <td>
            {p.citations.slice(0, 2).map((c, j) => (
              <CitationChip key={j} c={c} compact />
            ))}
          </td>
        </tr>
      ) : (
        <tr key={`gap-${i}`}>
          <td colSpan={3} className="muted">
            …
          </td>
        </tr>
      ),
    );
  return (
    <div className="v3-rel-series" data-testid="relation-series">
      <table className="v3-cells">
        <caption>
          {s.from} → {s.to}
          {s.facet && s.facet !== 'section=TOTAL' ? ` · ${s.facet.replace('section=', 'section SH ')}` : ''} <span className="muted">({s.unit ?? 'unité non précisée'}, tel que rapporté)</span>
        </caption>
        <thead>
          <tr>
            <th scope="col">Année</th>
            <th scope="col">Valeur</th>
            <th scope="col">Source</th>
          </tr>
        </thead>
        <tbody>{body(head)}</tbody>
      </table>
      {rows.length > MAX_SERIES_ROWS && (
        <details>
          <summary>Toutes les années ({rows.length})</summary>
          <table className="v3-cells">
            <tbody>{body(rows)}</tbody>
          </table>
        </details>
      )}
    </div>
  );
}

function VoteTable({ rows }: { rows: VoteRow[] }) {
  const decade = rows.filter((r) => r.grain !== 'year');
  const yearly = rows.filter((r) => r.grain === 'year');
  const table = (list: VoteRow[]) => (
    <table className="v3-cells" data-testid="vote-table">
      <thead>
        <tr>
          <th scope="col">Période</th>
          <th scope="col">Votes</th>
          <th scope="col">Part de votes identiques</th>
          <th scope="col">Votes identiques / votes communs</th>
          <th scope="col">Source</th>
        </tr>
      </thead>
      <tbody>
        {list.map((r) => (
          <tr key={r.key} data-period={r.period} data-issue={r.issue}>
            <th scope="row">{r.period}</th>
            <td>{ISSUE_LABEL[r.issue] ?? r.issue}</td>
            <td className="v3-num" title={r.share}>
              {r.shareShown}
            </td>
            <td className="v3-num">{r.nIdentical !== null && r.nCommon !== null ? `${r.nIdentical} / ${r.nCommon}` : <span className="v3-missing">effectifs non fournis</span>}</td>
            <td>
              {r.citations.slice(0, 1).map((c, j) => (
                <CitationChip key={j} c={c} compact />
              ))}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
  return (
    <div className="v3-rel-votes">
      <p className="field-hint" data-testid="vote-caveat">
        {VOTE_CAVEAT}
      </p>
      {decade.length > 0 && table(decade)}
      {yearly.length > 0 && (
        <details>
          <summary>Par année ({yearly.length})</summary>
          {table(yearly)}
        </details>
      )}
    </div>
  );
}
