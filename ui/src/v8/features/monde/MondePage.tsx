import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { getLocations } from '../../../api/client';
import { memo as memoOnce, warningsFr } from '../../../v3/api/common';
import { LicenceList } from '../../../v3/components/Licence';
import { v8Links } from '../../contract';
import { Badge, EmptyState, ErrorState, MissingValue, Skeleton, SourceChip, VirtualTable, frValue, toNumber, type ColumnDef } from '../../shell/ui';
import { getMap, type MapData, type MapResponse, type Point, type Rights, type Selector } from './api';
import { FilterBar, IndicatorPicker } from './IndicatorPicker';
import type { Fill } from './MondeMap';
import { PeriodBar } from './PeriodBar';
import { PlacePanel } from './PlacePanel';
import { usePlaceNames } from './places';
import { classColors, classify, colorOf, effectiveMode, legendRows, SCALE_LABEL } from './scale';
import { Scatter, type ScatterPoint } from './Scatter';
import { patchParams, readState, selectorKey, writeSelector } from './url';
import { dataOf, useAsync, useDark } from './useAsync';
import './monde.css';

const MondeMap = lazy(() => import('./MondeMap'));
const locations = memoOnce(() => getLocations());

export function indicatorLabel(m: MapData['meta'] | null | undefined): string {
  if (!m) return '';
  return m.label ?? m.concept?.labels.fr ?? m.concept?.labels.en ?? m.measure;
}

export function missingText(d: MapData, reason: string): string {
  return d.missing_labels[reason] ?? reason.replace(/^provider:/, '').replace(/_/g, ' ');
}

function rightsOf(d: MapData, p: Point): Rights | null {
  return p.lic !== null ? (d.licences[p.lic] ?? null) : null;
}

export function valueText(p: Point, unit?: string | null): string {
  if (p.v !== null) return `${frValue(p.v, p.unit ?? unit)}${p.unit && p.unit !== '1' ? ` ${p.unit}` : ''}`;
  return p.t ?? '';
}

export default function MondePage() {
  const [params, setParams] = useSearchParams();
  const st = readState(params);
  const dark = useDark();
  const { nameOf } = usePlaceNames();
  const [playing, setPlaying] = useState(false);
  const [hoverIso, setHoverIso] = useState<string | null>(null);

  const set = useCallback(
    (patch: Record<string, string | null>, replace = true) => setParams((p) => patchParams(p, patch), { replace }),
    [setParams],
  );
  const setSel = useCallback(
    (axis: '' | 'y', s: Selector | null) =>
      setParams(
        (p) => {
          const n = new URLSearchParams(p);
          writeSelector(n, axis, s);
          if (axis === '') {
            n.delete('period');
            n.delete('prec');
          }
          return n;
        },
        { replace: false },
      ),
    [setParams],
  );

  const xKey = st.x ? `${selectorKey(st.x)}#${st.period}#${st.prec}#${st.level}#${st.proj}` : null;
  const xa = useAsync<MapResponse>(xKey, () => getMap(st.x!, { period: st.period, precision: st.prec, level: st.level, projections: st.proj }));
  const xr = dataOf(xa);
  const xd = xr?.data ?? null;
  const biv = st.mode === 'bivarie';
  const yKey = biv && st.y && xd?.period ? `${selectorKey(st.y)}#${xd.period}#${xd.precision}#${st.proj}` : null;
  const ya = useAsync<MapResponse>(yKey, () => getMap(st.y!, { period: xd!.period, precision: xd!.precision, projections: st.proj }));
  const yd = dataOf(ya)?.data ?? null;
  const geo = useAsync('geo', () => locations());

  useEffect(() => {
    document.title = xd ? `${indicatorLabel(xd.meta)} ${xd.period_label ?? ''} · Monde` : 'Monde';
  }, [xd]);

  // classes from the slice's observed values (projections never shape the scale unless nothing else exists)
  const nums = useMemo(() => {
    if (!xd) return [];
    const obs = xd.values.filter((v) => !v.proj && v.v !== null).map((v) => Number(v.v));
    return obs.length ? obs : xd.values.filter((v) => v.v !== null).map((v) => Number(v.v));
  }, [xd]);
  const mode = effectiveMode(st.scale, xd?.scale_hint ?? 'quantile', nums);
  const classes = useMemo(() => classify(nums, mode), [nums, mode]);
  const colors = useMemo(() => classColors(classes, dark), [classes, dark]);
  const textCats = useMemo(() => {
    if (!xd || nums.length) return null;
    const counts = new Map<string, number>();
    for (const v of xd.values) if (v.t) counts.set(v.t, (counts.get(v.t) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [xd, nums.length]);
  const CAT = ['var(--v8-cat-1)', 'var(--v8-cat-2)', 'var(--v8-cat-3)'];

  const fills = useMemo(() => {
    const m = new Map<string, Fill>();
    if (!xd) return m;
    for (const miss of xd.missing) m.set(miss.geo, { state: 'missing' });
    for (const v of xd.values) {
      if (!v.iso3) continue;
      let color: string;
      if (v.v !== null) color = colorOf(classes, colors, Number(v.v));
      else {
        const i = textCats?.findIndex(([t]) => t === v.t) ?? -1;
        color = i >= 0 && i < 3 ? (dark ? ['#7d95ff', '#f0934a', '#3fc2a3'] : ['#3653d3', '#d9711c', '#138a72'])[i]! : dark ? '#5b6270' : '#9aa3ae';
      }
      m.set(v.iso3, { state: v.proj ? 'proj' : 'value', color });
    }
    return m;
  }, [xd, classes, colors, textCats, dark]);

  const byIso = useMemo(() => new Map((xd?.values ?? []).map((v) => [v.iso3 ?? v.geo ?? '', v])), [xd]);
  const missBy = useMemo(() => new Map((xd?.missing ?? []).map((m) => [m.geo, m.reason])), [xd]);

  const renderTip = useCallback(
    (iso3: string, polyName: string) => {
      if (!xd) return null;
      const v = byIso.get(iso3);
      const name = nameOf(iso3) || polyName;
      if (!v) {
        const r = missBy.get(iso3);
        return (
          <>
            <strong>{name}</strong>
            <span className="v8m-tip-miss">manquant : {r ? missingText(xd, r) : 'hors du référentiel de cette carte'}</span>
            <span className="v8-muted">{xd.period_label}</span>
          </>
        );
      }
      const lic = rightsOf(xd, v);
      return (
        <>
          <strong>{name}</strong>
          <span className="v8m-tip-val v8-num">{valueText(v, xd.meta.unit)}</span>
          {v.proj && <span className="v8m-proj-pill">projection — pas une observation</span>}
          <span>
            {v.p !== xd.period ? `période ${v.ps} → ${v.pe}` : xd.period_label}
            {v.basis && v.basis !== 'calendar_year' ? ` · ${v.basis.replace(/_/g, ' ')}` : ''}
          </span>
          <span className="v8-muted">source {v.series}</span>
          {lic && (
            <span className="v8-muted">
              licence {lic.licence}
              {lic.non_commercial ? ' · non commerciale' : ''}
            </span>
          )}
        </>
      );
    },
    [xd, byIso, missBy, nameOf],
  );

  const scatter: ScatterPoint[] = useMemo(() => {
    if (!xd || !yd) return [];
    const ys = new Map(yd.values.map((v) => [v.iso3, v]));
    const out: ScatterPoint[] = [];
    for (const xv of xd.values) {
      const yv = xv.iso3 ? ys.get(xv.iso3) : undefined;
      const xn = toNumber(xv.v);
      const yn = toNumber(yv?.v);
      if (!xv.iso3 || !yv || xn === null || yn === null) continue;
      out.push({ iso3: xv.iso3, name: nameOf(xv.iso3), x: xn, y: yn, proj: xv.proj || yv.proj, xText: valueText(xv, xd.meta.unit), yText: valueText(yv, yd.meta.unit) });
    }
    return out;
  }, [xd, yd, nameOf]);

  const settled = xa.status === 'ok' && (!yKey || ya.status === 'ok');
  const hasProj = (xd?.periods ?? []).some((p) => p.proj > 0);

  return (
    <div className="v8-page v8m" data-testid="monde-page">
      <header className="v8m-head">
        <div className="v8m-titles">
          <h1 className="v8-page-title">Monde</h1>
          <div className="v8m-mode" role="tablist" aria-label="Mode d’affichage">
            {(
              [
                ['carte', 'Carte'],
                ['bivarie', 'Deux indicateurs'],
              ] as const
            ).map(([id, label]) => (
              <button key={id} type="button" role="tab" aria-selected={st.mode === id} className={`v8m-seg${st.mode === id ? ' is-on' : ''}`} onClick={() => set({ mode: id === 'carte' ? null : id }, false)}>
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="v8m-pickers">
          <div className="v8m-axis">
            {biv && <span className="v8m-axis-tag">X</span>}
            <IndicatorPicker
              current={xd ? indicatorLabel(xd.meta) : st.x ? (st.x.measure ?? st.x.series ?? null) : null}
              sublabel={xd ? `${xd.meta.table} · ${xd.meta.unit ?? 'unité non précisée'}` : null}
              onPick={(s) => setSel('', s)}
              hotkey="/"
            />
            <FilterBar sel={st.x?.table ? st.x : xd ? { table: xd.meta.table, measure: xd.meta.measure, filters: Object.fromEntries(xd.meta.filters.map((f) => [f.key, f.value])) } : null} onChange={(s) => setSel('', s)} />
          </div>
          {biv && (
            <div className="v8m-axis">
              <span className="v8m-axis-tag">Y</span>
              <IndicatorPicker
                current={yd ? indicatorLabel(yd.meta) : st.y ? (st.y.measure ?? st.y.series ?? null) : null}
                sublabel={yd ? `${yd.meta.table} · ${yd.meta.unit ?? 'unité non précisée'}` : 'second indicateur'}
                onPick={(s) => setSel('y', s)}
                testId="indicator-picker-y"
              />
              <FilterBar sel={st.y?.table ? st.y : null} onChange={(s) => setSel('y', s)} />
            </div>
          )}
        </div>
      </header>

      {!st.x && (
        <EmptyState title="Choisissez un indicateur" icon="◍">
          Tout indicateur de l’atlas (≈ 600 000 séries, 220 tables) : tapez <kbd className="v8m-kbd">/</kbd> puis un mot (chômage, PIB par habitant, émissions de CO2…) ou un code
          (NY.GDP.PCAP.CD). Chaque valeur garde sa source et sa licence ; une donnée absente est hachurée avec sa raison, jamais comptée comme zéro.
        </EmptyState>
      )}

      {st.x && xa.status === 'error' && !xd && <ErrorState error={xa.error} what="cette carte" onRetry={xa.reload} />}
      {st.x && !xd && xa.status === 'loading' && <Skeleton height={520} />}

      {xd && (
        <>
          <div className="v8m-controls">
            <PeriodBar
              periods={xd.periods}
              period={xd.period}
              precision={xd.precision}
              precisions={xd.meta.precisions}
              playing={playing}
              settled={settled}
              onPeriod={(c) => set({ period: c })}
              onPrecision={(p) => set({ prec: p, period: null }, false)}
              onPlay={setPlaying}
            />
            <div className="v8m-opts">
              <div className="v8m-segs" role="group" aria-label="Niveau géographique">
                <button type="button" className={`v8m-seg${st.level === 'adm0' ? ' is-on' : ''}`} aria-pressed={st.level === 'adm0'} onClick={() => set({ level: null })}>
                  Pays
                </button>
                <button
                  type="button"
                  className={`v8m-seg${st.level === 'adm1' ? ' is-on' : ''}`}
                  aria-pressed={st.level === 'adm1'}
                  disabled={xd.meta.levels.adm1 === 0 && st.level !== 'adm1'}
                  title={xd.meta.levels.adm1 === 0 ? 'Cette série n’a pas de valeurs infranationales (ADM1/NUTS)' : `${xd.meta.levels.adm1} unités infranationales`}
                  onClick={() => set({ level: 'adm1', sel: null })}
                >
                  Régions
                </button>
              </div>
              {!biv && nums.length > 0 && st.level === 'adm0' && (
                <label className="v8m-inline">
                  <span>Échelle</span>
                  <select className="v8-input" value={st.scale} onChange={(e) => set({ scale: e.target.value === 'auto' ? null : e.target.value })}>
                    <option value="auto">auto ({SCALE_LABEL[mode]})</option>
                    <option value="quantile">quantiles</option>
                    <option value="linear">linéaire</option>
                    <option value="log">logarithmique</option>
                    <option value="diverging">divergente</option>
                  </select>
                </label>
              )}
              {hasProj && (
                <label className="v8m-inline">
                  <span>Projections</span>
                  <select className="v8-input" value={st.proj} onChange={(e) => set({ proj: e.target.value === 'exclude' ? null : e.target.value })}>
                    <option value="exclude">masquées</option>
                    <option value="include">montrées à part (pointillés)</option>
                    <option value="only">seules</option>
                  </select>
                </label>
              )}
              {biv && (
                <>
                  <label className="v8m-check">
                    <input type="checkbox" checked={st.logx} onChange={(e) => set({ logx: e.target.checked ? '1' : null })} /> X log
                  </label>
                  <label className="v8m-check">
                    <input type="checkbox" checked={st.logy} onChange={(e) => set({ logy: e.target.checked ? '1' : null })} /> Y log
                  </label>
                </>
              )}
            </div>
          </div>

          <Answer d={xd} nameOf={nameOf} loading={xa.status === 'loading'} />

          {biv ? (
            <div className="v8m-biv">
              <section className="v8m-card v8m-biv-main" aria-label="Nuage de points">
                {!st.y && <EmptyState title="Choisissez le second indicateur (Y)">Le nuage croise les deux indicateurs pour les pays qui ont une valeur pour les deux à la même période.</EmptyState>}
                {st.y && ya.status === 'error' && <ErrorState error={ya.error} what="le second indicateur" onRetry={ya.reload} />}
                {st.y && yd && scatter.length === 0 && (
                  <EmptyState title="Aucun pays avec les deux valeurs">
                    {yd.meta.precisions[xd.precision] ? `Aucun pays n’a de valeur pour les deux indicateurs en ${xd.period_label}.` : `Le second indicateur n’existe pas à la précision « ${xd.precision} ».`}
                  </EmptyState>
                )}
                {yd && scatter.length > 0 && (
                  <>
                    <Scatter
                      points={scatter}
                      xLabel={indicatorLabel(xd.meta)}
                      yLabel={indicatorLabel(yd.meta)}
                      logx={st.logx}
                      logy={st.logy}
                      selected={st.sel}
                      onSelect={(iso) => set({ sel: iso }, false)}
                      onHover={setHoverIso}
                    />
                    <p className="v8m-note">
                      {scatter.length} pays avec les deux valeurs en {xd.period_label} · une corrélation n’est pas une cause ·{' '}
                      {scatter.some((p) => p.proj) ? 'points creux : au moins une coordonnée est une projection · ' : ''}
                      {(st.logx && scatter.some((p) => p.x <= 0)) || (st.logy && scatter.some((p) => p.y <= 0)) ? 'échelle log impossible (valeurs ≤ 0) : axe linéaire · ' : ''}
                      sources {xd.meta.series} ; {yd.meta.series}
                    </p>
                  </>
                )}
              </section>
              <aside className="v8m-biv-side">
                <MapBlock geo={geo} fills={fills} st={st} dark={dark} xd={xd} renderTip={renderTip} set={set} highlight={hoverIso} height={300} />
                {st.sel && <PlacePanel sel={st.x!} geo={st.sel} name={nameOf(st.sel)} onClose={() => set({ sel: null }, false)} precision={xd.precision} />}
              </aside>
            </div>
          ) : (
            <div className="v8m-body">
              <section className="v8m-card v8m-mapcol" aria-label="Carte">
                {st.level === 'adm1' ? (
                  <EmptyState title="Pas de géométrie infranationale" icon="▦">
                    REVA ne sert pas encore de contours ADM1/NUTS : les {xd.counts.values ?? 0} valeurs régionales sont listées dans le classement, avec leur code fournisseur.
                  </EmptyState>
                ) : (
                  <MapBlock geo={geo} fills={fills} st={st} dark={dark} xd={xd} renderTip={renderTip} set={set} highlight={null} height="clamp(300px, 58vh, 620px)" />
                )}
                {st.level === 'adm0' && <Legend d={xd} rows={nums.length ? legendRows(classes, colors, (x) => frValue(String(Number(x.toPrecision(4))), xd.meta.unit)) : (textCats ?? []).slice(0, 3).map(([t, n], i) => ({ color: CAT[i]!, label: t, count: n }))} mode={nums.length ? SCALE_LABEL[mode] : 'catégories'} />}
              </section>
              <div className="v8m-side">
                {st.sel && <PlacePanel sel={st.x!} geo={st.sel} name={nameOf(st.sel)} onClose={() => set({ sel: null }, false)} precision={xd.precision} />}
                <Ranking d={xd} nameOf={nameOf} sel={st.sel} onSel={(g) => set({ sel: g }, false)} level={st.level} />
              </div>
            </div>
          )}
          <Sources r={xr!} y={biv ? (dataOf(ya) ?? null) : null} />
        </>
      )}
    </div>
  );
}

function MapBlock({
  geo,
  fills,
  st,
  dark,
  xd,
  renderTip,
  set,
  highlight,
  height,
}: {
  geo: ReturnType<typeof useAsync<Awaited<ReturnType<typeof getLocations>>>>;
  fills: Map<string, Fill>;
  st: ReturnType<typeof readState>;
  dark: boolean;
  xd: MapData;
  renderTip: (iso3: string, name: string) => React.ReactNode;
  set: (patch: Record<string, string | null>, replace?: boolean) => void;
  highlight: string | null;
  height: number | string;
}) {
  if (geo.status === 'error') return <ErrorState error={geo.error} what="la géométrie d’affichage (/v1/locations)" onRetry={geo.reload} />;
  const g = dataOf(geo);
  if (!g) return <Skeleton height={typeof height === 'number' ? height : 460} />;
  return (
    <Suspense fallback={<Skeleton height={typeof height === 'number' ? height : 460} />}>
      <MondeMap
        collection={g.data}
        fills={fills}
        selected={st.sel}
        highlight={highlight}
        dark={dark}
        height={height}
        label={`Carte ${indicatorLabel(xd.meta)} ${xd.period_label ?? ''} : ${xd.counts.values ?? 0} pays avec une valeur, ${xd.missing.length} sans valeur (hachurés). Le classement donne les mêmes données.`}
        onSelect={(iso) => set({ sel: iso }, false)}
        renderTip={renderTip}
      />
    </Suspense>
  );
}

function Answer({ d, nameOf, loading }: { d: MapData; nameOf: (iso: string) => string; loading: boolean }) {
  const vals = d.values.filter((v) => v.v !== null && !v.proj);
  const sorted = [...vals].sort((a, b) => Number(b.v) - Number(a.v));
  const top = sorted[0];
  const bottom = sorted[sorted.length - 1];
  const world = d.aggregates.find((a) => a.geo === 'WLD' || a.geo === '1W' || a.geo === 'WORLD');
  const lic = d.licences;
  return (
    <section className={`v8m-answer${loading ? ' is-loading' : ''}`} aria-live="polite" data-testid="monde-answer">
      <div className="v8m-answer-main">
        <h2 className="v8m-answer-title">
          {indicatorLabel(d.meta)} <span className="v8m-answer-period">{d.period_label}</span>
          {d.projections === 'only' && <Badge kind="projection" />}
        </h2>
        <p className="v8m-answer-sub">
          {d.meta.concept?.definition ? `${d.meta.concept.definition.slice(0, 220)}${d.meta.concept.definition.length > 220 ? '…' : ''} ` : ''}
          <span className="v8-muted">
            {d.meta.unit ?? 'unité non précisée'} · série <code>{d.meta.series}</code>
          </span>
        </p>
      </div>
      <dl className="v8m-facts">
        <div>
          <dt>Lieux avec valeur</dt>
          <dd className="v8-num">{d.counts.values ?? 0}</dd>
        </div>
        <div>
          <dt>Sans valeur</dt>
          <dd className="v8-num">{d.level === 'adm0' ? d.missing.length : (d.counts.provider_missing ?? 0)}</dd>
        </div>
        {top && d.level === 'adm0' && (
          <div>
            <dt>Maximum</dt>
            <dd>
              {nameOf(top.iso3 ?? '')} <span className="v8-num">{valueText(top, d.meta.unit)}</span>
            </dd>
          </div>
        )}
        {bottom && bottom !== top && d.level === 'adm0' && (
          <div>
            <dt>Minimum</dt>
            <dd>
              {nameOf(bottom.iso3 ?? '')} <span className="v8-num">{valueText(bottom, d.meta.unit)}</span>
            </dd>
          </div>
        )}
        {world && (
          <div>
            <dt>Monde (agrégat du fournisseur)</dt>
            <dd className="v8-num">{valueText(world, d.meta.unit)}</dd>
          </div>
        )}
        {(d.counts.projection ?? 0) > 0 && (
          <div>
            <dt>
              <Badge kind="projection" />
            </dt>
            <dd className="v8-num">{d.counts.projection} lieux</dd>
          </div>
        )}
        {(d.counts.projection_hidden ?? 0) > 0 && (
          <div>
            <dt>Projections masquées</dt>
            <dd className="v8-num">{d.counts.projection_hidden}</dd>
          </div>
        )}
      </dl>
      <div className="v8m-answer-src">
        <SourceChip source={`${d.meta.source}/${d.meta.dataset}`} label={d.meta.source} licence={lic[0] ?? d.meta.licences[0] ?? null} period={d.period_label} details={[`série ${d.meta.series}`, d.meta.recorded ? `table enregistrée le ${d.meta.recorded}` : ''].filter(Boolean)} />
      </div>
    </section>
  );
}

function Legend({ d, rows, mode }: { d: MapData; rows: { color: string; label: string; count: number }[]; mode: string }) {
  const byReason = new Map<string, number>();
  for (const m of d.missing) byReason.set(m.reason, (byReason.get(m.reason) ?? 0) + 1);
  return (
    <div className="v8m-legend" data-testid="monde-legend" aria-label="Légende">
      <ul className="v8m-legend-scale" aria-label={`Classes (${mode})`}>
        {rows.map((r) => (
          <li key={r.color + r.label}>
            <span className="v8m-sw" style={{ background: r.color }} aria-hidden="true" />
            <span className="v8-num">{r.label}</span>
          </li>
        ))}
        {(d.counts.projection ?? 0) > 0 && (
          <li>
            <span className="v8m-sw v8m-sw-proj" aria-hidden="true" /> projection (pointillés)
          </li>
        )}
      </ul>
      <p className="v8m-legend-mode v8-muted">Classes : {mode} des valeurs observées de la période. La couleur ne porte que la classe ; la valeur exacte est au survol et dans le classement.</p>
      {byReason.size > 0 && (
        <ul className="v8m-legend-miss">
          {[...byReason.entries()]
            .sort((a, b) => b[1] - a[1])
            .map(([r, n]) => (
              <li key={r}>
                <span className="v8m-sw v8m-sw-miss" aria-hidden="true" /> <span className="v8-num">{n}</span> {missingText(d, r)}
              </li>
            ))}
        </ul>
      )}
    </div>
  );
}

interface Row {
  geo: string;
  iso3: string | null;
  name: string;
  point: Point | null;
  reason: string | null;
  num: number | null;
}

function Ranking({ d, nameOf, sel, onSel, level }: { d: MapData; nameOf: (iso: string) => string; sel: string | null; onSel: (g: string) => void; level: string }) {
  const [showMissing, setShowMissing] = useState(false);
  const rows: Row[] = useMemo(() => {
    const vals: Row[] = d.values.map((p) => ({
      geo: (level === 'adm1' ? p.geo : p.iso3) ?? '',
      iso3: p.iso3,
      name: level === 'adm1' ? `${p.geo} (${nameOf(p.iso3 ?? '')})` : nameOf(p.iso3 ?? ''),
      point: p,
      reason: null,
      num: toNumber(p.v),
    }));
    const miss: Row[] = showMissing ? d.missing.map((m) => ({ geo: m.geo, iso3: m.geo, name: nameOf(m.geo), point: null, reason: m.reason, num: null })) : [];
    return [...vals, ...miss];
  }, [d, nameOf, level, showMissing]);
  const columns: ColumnDef<Row>[] = useMemo(
    () => [
      {
        id: 'Lieu',
        header: 'Lieu',
        accessorFn: (r) => r.name,
        cell: ({ row }) => (
          <span className="v8m-rank-name">
            {row.original.name} <span className="v8-subtle v8-mono">{level === 'adm1' ? '' : row.original.geo}</span>
          </span>
        ),
      },
      {
        id: 'Valeur',
        header: `Valeur${d.meta.unit ? ` (${d.meta.unit})` : ''}`,
        accessorFn: (r) => r.num ?? r.point?.t ?? undefined,
        sortUndefined: 'last',
        meta: { numeric: true },
        cell: ({ row }) => {
          const p = row.original.point;
          if (!p) return <MissingValue reason={missingText(d, row.original.reason ?? '')} compact />;
          return (
            <span className={p.proj ? 'v8m-proj-val' : undefined} title={p.proj ? 'projection — pas une observation' : (p.v ?? p.t ?? '')}>
              {p.v !== null ? frValue(p.v, p.unit ?? d.meta.unit) : p.t}
              {p.proj ? ' ᴾ' : ''}
              {p.p !== d.period ? <span className="v8-subtle"> ({p.p})</span> : null}
            </span>
          );
        },
      },
      {
        id: 'Source',
        header: 'Source',
        enableSorting: false,
        meta: { width: 84 },
        cell: ({ row }) => {
          const p = row.original.point;
          if (!p) return null;
          return <SourceChip source={p.series ?? d.meta.series} label={d.meta.source} licence={rightsOf(d, p)} period={`${p.ps} → ${p.pe}`} details={p.raw ? [`artefact brut ${p.raw.slice(0, 12)}…`] : []} />;
        },
      },
    ],
    [d, level],
  );
  return (
    <section className="v8m-card v8m-rank" aria-label="Classement">
      <header className="v8m-rank-head">
        <h3>
          Classement <span className="v8-muted">{d.period_label}</span>
        </h3>
        {d.missing.length > 0 && level === 'adm0' && (
          <label className="v8m-check">
            <input type="checkbox" checked={showMissing} onChange={(e) => setShowMissing(e.target.checked)} /> + {d.missing.length} sans valeur
          </label>
        )}
      </header>
      <VirtualTable
        data={rows}
        columns={columns}
        caption={`Classement ${indicatorLabel(d.meta)} ${d.period_label ?? ''}`}
        height="clamp(260px, 52vh, 560px)"
        rowHeight={30}
        getRowId={(r) => r.geo}
        initialSorting={[{ id: 'Valeur', desc: true }]}
        onRowClick={(r) => r.iso3 && onSel(level === 'adm1' ? r.geo : r.iso3)}
        isRowActive={(r) => r.geo === sel}
        empty={<EmptyState title="Aucune valeur pour cette période" compact />}
      />
      {sel && level === 'adm0' && (
        <p className="v8m-rank-foot">
          <Link to={v8Links.entity('country', sel)}>Fiche {nameOf(sel)} →</Link>
        </p>
      )}
    </section>
  );
}

function Sources({ r, y }: { r: MapResponse; y: MapResponse | null }) {
  const lic = [...r.data.licences, ...(y?.data.licences ?? [])].map((l) => ({ ...l, source: l.source ?? r.data.meta.source }));
  const warns = warningsFr([...r.warnings, ...(y?.warnings ?? [])]);
  return (
    <footer className="v8m-sources">
      <LicenceList licences={lic} title="Licences restrictives des valeurs affichées" />
      {warns.length > 0 && (
        <ul className="v8m-warn">
          {warns.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      <p className="v8-muted">
        Instantané atlas {r.snapshot.snapshot_sha256.slice(0, 12)}… ({r.snapshot.origin}) · arrêté au {r.snapshot.knowledge_cutoff.slice(0, 10)} · une série par carte : aucune valeur
        mélangée entre sources, imputée ou mise à zéro.
        {r.took_ms != null ? ` · ${Math.round(r.took_ms)} ms` : ''}
      </p>
    </footer>
  );
}
