import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { warningsFr } from '../../../v3/api/common';
import { LicenceList } from '../../../v3/components/Licence';
import { v8Links } from '../../contract';
import { Badge, EmptyState, ErrorState, MissingValue, Skeleton, SourceChip, frNumber } from '../../shell/ui';
import { getCatalog, getMulti, getVintages, type MultiResponse, type MultiSeries, type Point, type VintagesResponse } from '../monde/api';
import { IndicatorPicker } from '../monde/IndicatorPicker';
import { usePlaceNames } from '../monde/places';
import { mondeHref } from '../monde/url';
import { dataOf, useAsync } from '../monde/useAsync';
import { CompareChart, type ChartPlace } from './Chart';
import { buildCsv, downloadCsv } from './csv';
import { parseCompareQuery } from './parse';
import { allowed, commonBase, popScale, shownUnit, transformPlace, TRANSFORM_LABEL, type Shown, type TransformId } from './transforms';
import '../monde/monde.css';
import './comparer.css';

export const MAX_PLACES = 6;
export const MAX_SERIES = 6;
const COLORS = ['var(--v8-cat-1)', 'var(--v8-cat-2)', 'var(--v8-cat-3)', 'var(--v8-cat-4)', 'var(--v8-cat-5)', 'var(--v8-cat-6)'];
const TRANSFORMS: TransformId[] = ['none', 'index', 'yoy', 'percap'];

function resolvedColor(v: string): string {
  const m = /^var\((--[^)]+)\)$/.exec(v);
  if (!m) return v;
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(m[1]!).trim() || '#3653d3';
  } catch {
    return '#3653d3';
  }
}

export function seriesLabel(s: MultiSeries): string {
  return s.meta.label ?? s.meta.concept?.labels.fr ?? s.meta.concept?.labels.en ?? s.meta.measure;
}

/** Vintage points → atlas-like points (each names its vintage and dataset). */
function vintagePoints(v: VintagesResponse['data'], mode: string, iso: string): Point[] | null {
  const src = mode === 'first' ? v.first[iso] : v.by_vintage[mode]?.[iso];
  if (!src) return null;
  return src.map((q) => ({
    p: q.p,
    ps: q.ps,
    pe: null,
    basis: 'calendar_year',
    v: q.v,
    t: null,
    unit: q.unit,
    proj: q.proj,
    miss: null,
    flags: { vintage: q.vintage, available_at: q.available_at },
    geo: iso,
    iso3: iso,
    series: `${q.dataset ?? 'millésime'} · v=${q.vintage}`,
    raw: null,
    table_sha: null,
    lic: null,
  }));
}

export default function ComparerPage() {
  const [params, setParams] = useSearchParams();
  const { nameOf, places } = usePlaceNames();
  const series = params.getAll('s').slice(0, MAX_SERIES);
  const geos = (params.get('g') ?? '')
    .split(',')
    .map((x) => x.trim().toUpperCase())
    .filter((x) => /^[A-Z]{3}$/.test(x))
    .slice(0, MAX_PLACES);
  const t = (TRANSFORMS.includes(params.get('t') as TransformId) ? params.get('t') : 'none') as TransformId;
  const view = params.get('vue') === 'tableau' ? 'tableau' : 'graphiques';
  const vint = params.get('vint') ?? 'latest';
  const q = params.get('q');
  const [cursorX, setCursorX] = useState<number | null>(null);
  const [resolving, setResolving] = useState<string | null>(null);
  const [addPlace, setAddPlace] = useState('');

  const update = useCallback(
    (fn: (p: URLSearchParams) => void, replace = false) =>
      setParams(
        (p) => {
          const n = new URLSearchParams(p);
          fn(n);
          return n;
        },
        { replace },
      ),
    [setParams],
  );

  // "comparer FRA DEU inflation" (palette) → places + the first catalog series of the remaining words
  useEffect(() => {
    if (!q || places.length === 0) return;
    const parsed = parseCompareQuery(q, places.map((p) => ({ ...p, frName: nameOf(p.iso3) })));
    const done = (s: string | null, msg: string | null) => {
      setResolving(msg);
      update((n) => {
        n.delete('q');
        const g = [...new Set([...geos, ...parsed.geos])].slice(0, MAX_PLACES);
        if (g.length) n.set('g', g.join(','));
        if (s && !n.getAll('s').includes(s)) n.append('s', s);
      }, true);
    };
    if (!parsed.rest.trim()) {
      done(null, null);
      return;
    }
    setResolving(`Recherche de « ${parsed.rest} »…`);
    getCatalog(parsed.rest, null)
      .then((r) => {
        const hit = r.hits.find((h) => h.series && h.in_snapshot);
        done(hit?.series ?? null, hit ? null : `Aucun indicateur chargé ne correspond à « ${parsed.rest} ».`);
      })
      .catch(() => done(null, `La recherche de « ${parsed.rest} » a échoué.`));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, places.length]);

  const needPop = t === 'percap';
  const key = series.length && geos.length ? `${series.join('|')}#${geos.join(',')}#${needPop}` : null;
  const a = useAsync<MultiResponse>(key, () => getMulti(series, geos, needPop));
  const r = dataOf(a);
  const vintSeries = r?.data.series.find((s) => s.vintages_available) ?? null;
  const va = useAsync<VintagesResponse>(vintSeries && vint !== 'latest' ? `${vintSeries.meta.series}#${geos.join(',')}` : null, () => getVintages(vintSeries!.meta.series, geos));
  const vd = dataOf(va)?.data ?? null;
  const vintList = useAsync<VintagesResponse>(vintSeries ? `list:${vintSeries.meta.series}#${geos.join(',')}` : null, () => getVintages(vintSeries!.meta.series, geos));
  const vintOptions = dataOf(vintList)?.data.vintages ?? [];

  useEffect(() => {
    document.title = r ? `${r.data.series.map(seriesLabel).join(', ')} · Comparer` : 'Comparer';
  }, [r]);

  const panels = useMemo(() => {
    if (!r) return [];
    const pop = r.data.population;
    const scale = popScale(pop?.concept?.unit);
    return r.data.series.map((s) => {
      const usesVintage = s.vintages_available && vint !== 'latest' && vd?.available;
      const placesPts: Record<string, Point[]> = {};
      for (const g of r.data.geos) placesPts[g] = (usesVintage && vd ? vintagePoints(vd, vint, g) : null) ?? (usesVintage ? [] : (s.places[g] ?? []));
      const eff: MultiSeries = { ...s, places: placesPts };
      const ok = allowed(eff, t);
      const tt: TransformId = ok.ok ? t : 'none';
      const base = tt === 'index' ? commonBase(placesPts) : null;
      const shown: Record<string, Shown[]> = {};
      for (const g of r.data.geos) shown[g] = transformPlace(placesPts[g] ?? [], tt, { base, pop: pop?.places[g] ?? null, popScale: scale });
      return { s, eff, tt, ok, base, shown, usesVintage: !!usesVintage };
    });
  }, [r, t, vint, vd]);

  const xRange: [number, number] = useMemo(() => {
    const xs = panels.flatMap((p) => Object.values(p.shown).flatMap((pts) => pts.filter((q) => q.y !== null).map((q) => q.x)));
    if (!xs.length) return [0, 1];
    const lo = Math.min(...xs);
    const hi = Math.max(...xs);
    return lo === hi ? [lo - 1, hi + 1] : [lo, hi];
  }, [panels]);

  const anyAllowed = (id: TransformId) => (r ? r.data.series.some((s) => allowed(s, id).ok) : false);
  const reasonOf = (id: TransformId) =>
    r
      ? r.data.series
          .map((s) => allowed(s, id))
          .find((x) => !x.ok)?.reason
      : null;

  const exportCsv = () => {
    if (!r) return;
    const csv = buildCsv(
      panels.map((p) => ({ series: p.s, transform: p.tt, unit: shownUnit(p.s, p.tt), shown: p.shown, licences: r.data.licences, vintage: p.usesVintage ? vint : null })),
      r.data.geos,
      nameOf,
      r.data.population,
    );
    downloadCsv(csv, `reva-comparer-${r.data.geos.join('-')}-${new Date().toISOString().slice(0, 10)}.csv`);
  };

  const addGeo = (raw: string) => {
    const parsed = parseCompareQuery(raw, places.map((p) => ({ ...p, frName: nameOf(p.iso3) })));
    if (!parsed.geos.length) return false;
    update((n) => n.set('g', [...new Set([...geos, ...parsed.geos])].slice(0, MAX_PLACES).join(',')));
    return true;
  };

  return (
    <div className="v8-page v8c" data-testid="comparer-page">
      <header className="v8c-head">
        <h1 className="v8-page-title">Comparer</h1>
        <div className="v8c-picks">
          <div className="v8c-row" aria-label="Lieux comparés">
            {geos.map((g, i) => (
              <span key={g} className="v8c-chip">
                <span className="v8c-sw" style={{ background: COLORS[i] }} aria-hidden="true" />
                {nameOf(g)} <span className="v8-subtle v8-mono">{g}</span>
                <button type="button" aria-label={`Retirer ${nameOf(g)}`} onClick={() => update((n) => n.set('g', geos.filter((x) => x !== g).join(',')))}>
                  ✕
                </button>
              </span>
            ))}
            {geos.length < MAX_PLACES && (
              <form
                className="v8c-add"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (addGeo(addPlace)) setAddPlace('');
                }}
              >
                <input className="v8-input" list="v8c-places" value={addPlace} onChange={(e) => setAddPlace(e.target.value)} placeholder="Ajouter un pays (FRA, Allemagne…)" aria-label="Ajouter un pays" />
                <datalist id="v8c-places">
                  {places.map((p) => (
                    <option key={p.iso3} value={p.iso3}>
                      {nameOf(p.iso3)}
                    </option>
                  ))}
                </datalist>
              </form>
            )}
          </div>
          <div className="v8c-row" aria-label="Indicateurs comparés">
            {(r?.data.series ?? []).map((s) => (
              <span key={s.meta.series} className="v8c-chip v8c-chip-ind">
                {seriesLabel(s)} <span className="v8-subtle">{s.meta.table}</span>
                <button type="button" aria-label={`Retirer ${seriesLabel(s)}`} onClick={() => update((n) => {
                  const keep = n.getAll('s').filter((x) => x !== s.meta.series);
                  n.delete('s');
                  keep.forEach((x) => n.append('s', x));
                })}>
                  ✕
                </button>
              </span>
            ))}
            {series.length < MAX_SERIES && (
              <div className="v8c-addind">
                <IndicatorPicker
                  current={series.length ? 'Ajouter un indicateur' : 'Choisir un indicateur'}
                  onPick={(sel, hit) => {
                    const key = hit?.series ?? (sel.table && sel.measure ? `${sel.table}.${[sel.measure, ...Object.entries(sel.filters ?? {}).map(([k, v]) => `${k}=${v}`)].join('|')}` : null);
                    if (key) update((n) => !n.getAll('s').includes(key) && n.append('s', key));
                  }}
                  hotkey="/"
                  testId="comparer-picker"
                />
              </div>
            )}
          </div>
        </div>
      </header>

      {resolving && <p className="v8c-resolving">{resolving}</p>}

      {(!series.length || !geos.length) && !q && (
        <EmptyState title={!geos.length ? 'Ajoutez des pays' : 'Ajoutez un indicateur'} icon="⇆">
          Comparez jusqu’à {MAX_PLACES} pays sur {MAX_SERIES} indicateurs, alignés dans le temps. Exemples :{' '}
          <Link to="/v8/comparer?g=FRA,DEU,ITA&s=imf.weo.PCPIPCH&s=wb.wdi_econ.NY.GDP.PCAP.CD">France, Allemagne, Italie — inflation et PIB par habitant</Link> ·{' '}
          <Link to="/v8/comparer?g=USA,CHN,IND&s=wb.wdi_econ.NY.GDP.MKTP.CD&t=percap">États-Unis, Chine, Inde — PIB par habitant (calcul REVA)</Link>. Dans la palette :{' '}
          <kbd className="v8m-kbd">comparer FRA DEU inflation</kbd>.
        </EmptyState>
      )}

      {key && a.status === 'error' && !r && <ErrorState error={a.error} what="la comparaison" onRetry={a.reload} />}
      {key && !r && a.status === 'loading' && <Skeleton height={420} />}

      {r && (
        <>
          <div className="v8m-controls v8c-controls">
            <div className="v8m-segs" role="group" aria-label="Transformation">
              {TRANSFORMS.map((id) => {
                const ok = id === 'none' || anyAllowed(id);
                return (
                  <button
                    key={id}
                    type="button"
                    className={`v8m-seg${t === id ? ' is-on' : ''}`}
                    aria-pressed={t === id}
                    disabled={!ok}
                    title={ok ? TRANSFORM_LABEL[id] : `Indisponible : ${reasonOf(id) ?? 'non applicable'}`}
                    onClick={() => update((n) => (id === 'none' ? n.delete('t') : n.set('t', id)))}
                  >
                    {TRANSFORM_LABEL[id]}
                  </button>
                );
              })}
            </div>
            <div className="v8m-segs" role="tablist" aria-label="Vue">
              {(['graphiques', 'tableau'] as const).map((v) => (
                <button key={v} type="button" role="tab" aria-selected={view === v} className={`v8m-seg${view === v ? ' is-on' : ''}`} onClick={() => update((n) => (v === 'graphiques' ? n.delete('vue') : n.set('vue', v)), true)}>
                  {v === 'graphiques' ? 'Graphiques' : 'Tableau'}
                </button>
              ))}
            </div>
            {vintSeries && (
              <label className="v8m-inline">
                <span>Millésime</span>
                <select className="v8-input" value={vint} onChange={(e) => update((n) => (e.target.value === 'latest' ? n.delete('vint') : n.set('vint', e.target.value)))}>
                  <option value="latest">dernière version (instantané)</option>
                  <option value="first">telle que publiée la première fois</option>
                  {vintOptions.map((v) => (
                    <option key={v.vintage} value={v.vintage}>
                      millésime {v.vintage}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <button type="button" className="v8-btn v8-btn-sm" onClick={exportCsv} title="Exporter exactement ce qui est affiché, avec les colonnes de source">
              ⤓ CSV
            </button>
          </div>

          {t === 'percap' && r.data.population && (
            <p className="v8c-calc" data-testid="percap-note">
              <Badge kind="estimate" title="Calcul REVA : quotient de deux séries sourcées, pas une valeur publiée.">calcul REVA</Badge> valeur ÷ population de la même année et du même pays — population : «{' '}
              {r.data.population.concept?.labels.fr ?? r.data.population.concept?.id} » (meilleure source atlas par année :{' '}
              {[...new Set(Object.values(r.data.population.places).flatMap((ps) => ps.map((p) => p.series)).filter(Boolean))].join(', ') || 'aucune'}) ; numérateur : la série
              affichée. Les deux sources et licences restent citées ; une année sans population est manquante, jamais comblée.
            </p>
          )}

          {panels.map((p) => (
            <section key={p.s.meta.series} className="v8m-card v8c-panel" aria-label={seriesLabel(p.s)}>
              <header className="v8c-panel-head">
                <div className="v8c-panel-title">
                  <h2>{seriesLabel(p.s)}</h2>
                  <span className="v8-muted">
                    {shownUnit(p.s, p.tt) || 'unité non précisée'} · {p.s.precision === 'year' ? 'annuel' : p.s.precision}
                    {p.tt === 'index' && p.base ? ` · base 100 en ${p.base}` : ''}
                  </span>
                  {p.usesVintage && <Badge kind="info" title="Valeurs telles que publiées dans un millésime antérieur du fournisseur (pas la dernière révision).">{vint === 'first' ? 'première publication' : `millésime ${vint}`}</Badge>}
                  {Object.values(p.shown).some((pts) => pts.some((x) => x.proj && x.y !== null)) && <Badge kind="projection" />}
                </div>
                <div className="v8c-panel-src">
                  <SourceChip source={`${p.s.meta.source}/${p.s.meta.dataset}`} label={p.s.meta.source} licence={p.s.meta.licences[0] ?? null} details={[`série ${p.s.meta.series}`]} />
                  <Link className="v8c-link" to={mondeHref({ series: p.s.meta.series })}>
                    carte →
                  </Link>
                </div>
              </header>
              {t !== 'none' && p.tt === 'none' && (
                <p className="v8c-untransformed">
                  Non transformé ({TRANSFORM_LABEL[t].toLowerCase()} indisponible) : {p.ok.reason}
                </p>
              )}
              {p.usesVintage && vd && !vd.available && <p className="v8c-untransformed">Millésimes indisponibles : {vd.reason}</p>}
              {view === 'graphiques' ? (
                <CompareChart
                  places={r.data.geos.map((g, i) => ({ iso3: g, name: nameOf(g), color: resolvedColor(COLORS[i]!), points: p.shown[g] ?? [] }) satisfies ChartPlace)}
                  xRange={xRange}
                  annual={p.s.precision === 'year'}
                  unit={shownUnit(p.s, p.tt)}
                  label={seriesLabel(p.s)}
                  syncKey="v8-comparer"
                  cursorX={cursorX}
                  onCursor={setCursorX}
                />
              ) : (
                <ValuesTable geos={r.data.geos} nameOf={nameOf} shown={p.shown} />
              )}
              <MissingSummary geos={r.data.geos} nameOf={nameOf} shown={p.shown} />
            </section>
          ))}

          <footer className="v8m-sources">
            <LicenceList licences={[...r.data.licences, ...(r.data.population?.licences ?? [])].map((l) => ({ ...l, source: l.source ?? '' }))} title="Licences restrictives des valeurs affichées" />
            {warningsFr(r.warnings).map((w) => (
              <p key={w}>{w}</p>
            ))}
            <p className="v8-muted">
              Instantané atlas {r.snapshot.snapshot_sha256.slice(0, 12)}… · chaque série est celle d’une seule source (aucun mélange) · les projections sont en tirets et ne sont jamais
              reliées aux observations · une valeur manquante est un trou, jamais un zéro.
              {r.data.geos.length === 1 && (
                <>
                  {' '}
                  <Link to={v8Links.entity('country', r.data.geos[0]!)}>Fiche {nameOf(r.data.geos[0]!)} →</Link>
                </>
              )}
            </p>
          </footer>
        </>
      )}
    </div>
  );
}

function ValuesTable({ geos, nameOf, shown }: { geos: string[]; nameOf: (g: string) => string; shown: Record<string, Shown[]> }) {
  const xs = new Map<string, number>();
  for (const g of geos) for (const x of shown[g] ?? []) xs.set(x.p, x.x);
  const periods = [...xs.entries()].sort((a, b) => b[1] - a[1]).map(([p]) => p);
  const by = new Map(geos.map((g) => [g, new Map((shown[g] ?? []).map((x) => [x.p, x]))]));
  return (
    <div className="v8c-table" tabIndex={0}>
      <table>
        <thead>
          <tr>
            <th scope="col">Période</th>
            {geos.map((g) => (
              <th key={g} scope="col" className="v8-num">
                {nameOf(g)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {periods.map((p) => {
            return (
              <tr key={p}>
                <th scope="row" className="v8-num">
                  {p}
                </th>
                {geos.map((g) => {
                  const x = by.get(g)?.get(p);
                  return (
                    <td key={g} className={`v8-num${x?.proj ? ' is-proj' : ''}`} title={x ? [x.src?.v ? `valeur rapportée ${x.src.v} ${x.src.unit ?? ''}` : '', x.note ?? '', x.src?.series ?? ''].filter(Boolean).join(' · ') : ''}>
                      {!x ? <MissingValue reason="pas de cellule pour cette période" compact /> : x.y !== null ? `${frNumber(x.y, Math.abs(x.y) < 10 ? 3 : 1)}${x.proj ? ' ᴾ' : ''}` : <MissingValue reason={x.miss} compact />}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function MissingSummary({ geos, nameOf, shown }: { geos: string[]; nameOf: (g: string) => string; shown: Record<string, Shown[]> }) {
  const empty = geos.filter((g) => !(shown[g] ?? []).some((x) => x.y !== null));
  if (!empty.length) return null;
  return (
    <p className="v8c-missing">
      <MissingValue reason="aucune valeur pour" compact /> {empty.map(nameOf).join(', ')} — cette série ne les couvre pas (absent, pas zéro).
    </p>
  );
}
