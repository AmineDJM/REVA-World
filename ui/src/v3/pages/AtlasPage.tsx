import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { useVirtualizer } from '@tanstack/react-virtual';
import { getLocations } from '../../api/client';
import { useApi } from '../../lib/useApi';
import { warningsFr } from '../api/common';
import { conceptLabel, getAtlasCatalog, getAtlasCoverage, getAtlasMap, type AtlasConcept, type AtlasMapResponse } from '../api/atlas';
import { FixtureBadge, MissingCell, ProviderDeclaredBadge } from '../components/Honesty';
import { V3Empty, V3Error, V3Loading } from '../components/States';
import { buildScale, cellText, countryStates, iso3OfFeatureKey, legendRows, NO_SOURCE_REASON, OUTSIDE, ranking } from '../lib/choropleth';
import { flagLines, frInt } from '../lib/format';
import { domainLabel } from '../lib/domains';
import { CitationChip } from '../components/CitationChip';
import { citeAtlasCell } from '../lib/citations';

const AtlasMap = lazy(() => import('../components/AtlasMap'));

type MapState = { status: 'idle' } | { status: 'loading'; prev?: AtlasMapResponse } | { status: 'ok'; data: AtlasMapResponse } | { status: 'error'; error: unknown };

export default function AtlasPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const concept = params.get('concept');
  const yearParam = params.get('annee');
  const concepts = useApi(() => getAtlasCatalog(), []);
  const coverage = useApi((s) => getAtlasCoverage({ concept: concept! }, s), [concept], !!concept);
  const locations = useApi((s) => getLocations(undefined, s), []);
  const [filter, setFilter] = useState('');
  const [playing, setPlaying] = useState(false);
  const [map, setMap] = useState<MapState>({ status: 'idle' });
  const cache = useRef(new Map<string, AtlasMapResponse>());

  const cov = coverage.status === 'ok' ? coverage.data.data.find((c) => c.concept_id === concept) : undefined;
  const yMin = cov?.year_min ?? null;
  const yMax = cov?.year_max ?? null;
  const year = yearParam && /^-?\d+$/.test(yearParam) ? Number(yearParam) : yMax;

  const setParam = (patch: Record<string, string | null>, replace = false) => {
    const p = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) p.delete(k);
      else p.set(k, v);
    }
    setParams(p, { replace });
  };

  useEffect(() => {
    if (!concept || year === null) {
      setMap({ status: 'idle' });
      return;
    }
    const key = `${concept}@${year}`;
    const hit = cache.current.get(key);
    if (hit) {
      setMap({ status: 'ok', data: hit });
      return;
    }
    const ctrl = new AbortController();
    setMap((m) => ({ status: 'loading', prev: m.status === 'ok' ? m.data : m.status === 'loading' ? m.prev : undefined }));
    getAtlasMap(concept, year, ctrl.signal)
      .then((d) => {
        cache.current.set(key, d);
        if (!ctrl.signal.aborted) setMap({ status: 'ok', data: d });
      })
      .catch((error: unknown) => {
        if (!ctrl.signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) {
          setMap({ status: 'error', error });
          setPlaying(false);
        }
      });
    return () => ctrl.abort();
  }, [concept, year]);

  // Play: advance one year after each loaded map, stop at the last covered year.
  useEffect(() => {
    if (!playing || map.status !== 'ok' || year === null || yMax === null) return;
    if (year >= yMax) {
      setPlaying(false);
      return;
    }
    const t = setTimeout(() => setParam({ annee: String(year + 1) }, true), 900);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, map.status, year, yMax]);

  const shown = map.status === 'ok' ? map.data : map.status === 'loading' ? map.prev : undefined;
  const scale = useMemo(() => (shown ? buildScale(shown.data.values) : null), [shown]);
  const states = useMemo(() => (shown ? countryStates(shown.data.values, shown.data.countries_without_value, scale) : new Map()), [shown, scale]);
  const names = useMemo(() => {
    const m = new Map<string, string>();
    if (locations.status === 'ok')
      for (const f of locations.data.data.features) {
        const iso = iso3OfFeatureKey(f.properties.feature_key);
        if (iso && !m.has(iso)) m.set(iso, f.properties.name ?? f.properties.entity_label ?? iso);
      }
    return m;
  }, [locations.status, locations.data]);
  const nameOf = (iso: string) => names.get(iso) ?? iso;

  const conceptList = concepts.status === 'ok' ? concepts.data.data : [];
  const current: AtlasConcept | undefined = shown?.data.concept ?? conceptList.find((c) => c.id === concept);
  const filtered = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const list = f ? conceptList.filter((c) => c.id.toLowerCase().includes(f) || conceptLabel(c).toLowerCase().includes(f) || (c.labels.en ?? '').toLowerCase().includes(f)) : conceptList;
    const byDomain = new Map<string, AtlasConcept[]>();
    for (const c of list) byDomain.set(c.domain, [...(byDomain.get(c.domain) ?? []), c]);
    return [...byDomain.entries()].sort((a, b) => domainLabel(a[0]).localeCompare(domainLabel(b[0]), 'fr'));
  }, [conceptList, filter]);

  return (
    <section className="v3-page v3-atlas" aria-labelledby="atlas-title">
      <header className="v3-page-head">
        <h1 id="atlas-title">Atlas{current ? ` — ${conceptLabel(current)}` : ''}</h1>
        <p className="v3-lede">Toute variable du catalogue, pour tous les pays et toutes les années disponibles. Une donnée manquante est hachurée avec sa raison, jamais comptée comme zéro.</p>
      </header>
      {concepts.status === 'loading' && <V3Loading what="du catalogue" />}
      {concepts.status === 'error' && <V3Error error={concepts.error} what="Le catalogue de l’atlas" onRetry={concepts.reload} />}
      {concepts.status === 'ok' && (
        <div className="v3-toolbar">
          <div className="v3-concept-picker">
            <label>
              Chercher une variable
              <input type="search" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="chômage, salaire minimum, population…" />
            </label>
            <label>
              Variable ({frInt(concepts.data.total)} au catalogue)
              <select value={concept ?? ''} onChange={(e) => setParam({ concept: e.target.value || null, annee: null })} data-testid="concept-select">
                <option value="">— choisir —</option>
                {filtered.map(([domain, list]) => (
                  <optgroup key={domain} label={domainLabel(domain)}>
                    {list.map((c) => (
                      <option key={c.id} value={c.id}>
                        {conceptLabel(c)} ({c.unit})
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
          </div>
          {concept && yMin !== null && yMax !== null && year !== null && (
            <fieldset className="v3-year-filter">
              <legend>Année</legend>
              <button type="button" onClick={() => setPlaying((p) => !p)} aria-pressed={playing} disabled={yMin === yMax}>
                {playing ? '⏸ Pause' : '▶ Lecture'}
              </button>
              <input
                type="range"
                min={yMin}
                max={yMax}
                value={Math.min(yMax, Math.max(yMin, year))}
                onChange={(e) => {
                  setPlaying(false);
                  setParam({ annee: e.target.value }, true);
                }}
                aria-label="Année"
              />
              <output className="v3-year-out">{year}</output>
              <span className="muted">
                {yMin}–{yMax}
              </span>
            </fieldset>
          )}
        </div>
      )}
      {!concept && concepts.status === 'ok' && (
        <V3Empty>
          <p>
            <strong>Choisissez une variable</strong> pour afficher la carte du monde.
          </p>
        </V3Empty>
      )}
      {concept && coverage.status === 'error' && <V3Error error={coverage.error} what="La couverture de cette variable" onRetry={coverage.reload} />}
      {concept && coverage.status === 'ok' && !cov && (
        <V3Empty>
          <p>
            <strong>Aucune cellule chargée</strong> pour cette variable dans l’instantané atlas actuel. Sources déclarées : {current?.source_order.join(', ') || 'aucune'}.
          </p>
        </V3Empty>
      )}
      {concept && cov && yMin === null && (
        <V3Empty>
          <p>
            <strong>Aucune année couverte</strong> pour cette variable.
          </p>
        </V3Empty>
      )}
      {map.status === 'error' && <V3Error error={map.error} what={`La carte ${year ?? ''}`} />}
      {map.status === 'loading' && !shown && <V3Loading what="de la carte" />}
      {shown && (
        <div className="v3-atlas-body">
          <div className="v3-atlas-map">
            {locations.status === 'error' ? (
              <V3Error error={locations.error} what="La géométrie d’affichage (/v1/locations)">
                <p>Le classement ci-dessous reste complet.</p>
              </V3Error>
            ) : locations.status === 'ok' ? (
              <Suspense fallback={<V3Loading what="de la carte" />}>
                <AtlasMap
                  collection={locations.data.data}
                  states={states}
                  year={shown.data.year}
                  selected={null}
                  onSelect={(iso) => navigate(`/v3/pays/${iso}`)}
                  label={`Carte ${conceptLabel(shown.data.concept)} ${shown.data.year} : ${shown.data.countries_with_value} pays avec une valeur, ${shown.data.countries_without_value.length} sans valeur (hachurés). Le classement ci-dessous donne les mêmes données.`}
                />
              </Suspense>
            ) : (
              <V3Loading what="de la géométrie" />
            )}
            {map.status === 'loading' && <p className="v3-stage-stats">chargement de {year}…</p>}
          </div>
          <Legend data={shown} scaleRows={legendRows(scale, shown.data.concept.unit)} />
          <Ranking data={shown} nameOf={nameOf} />
        </div>
      )}
    </section>
  );
}

function Legend({ data, scaleRows }: { data: AtlasMapResponse; scaleRows: { color: string; label: string }[] }) {
  const d = data.data;
  const flagged = d.values.filter((v) => flagLines(v.flags).length > 0);
  const flagKinds = [...new Set(flagged.flatMap((v) => Object.keys(v.flags)))].filter((k) => k !== 'missing');
  return (
    <aside className="v3-legend" aria-label="Légende et sources" data-testid="atlas-legend">
      <h2>Légende — {d.year}</h2>
      <p className="v3-legend-def">
        <strong>{conceptLabel(d.concept)}</strong> ({d.concept.unit}) — {d.concept.definition} <ProviderDeclaredBadge />
      </p>
      <ul className="v3-legend-scale">
        {scaleRows.map((r) => (
          <li key={r.label}>
            <span className="v3-swatch" style={{ background: r.color }} aria-hidden="true" /> {r.label}
          </li>
        ))}
        <li>
          <span className="v3-swatch v3-swatch-gap" aria-hidden="true" /> manquant — {d.countries_without_value.length} pays : {NO_SOURCE_REASON}
        </li>
        <li>
          <span className="v3-swatch" style={{ background: OUTSIDE }} aria-hidden="true" /> géométrie hors référentiel des pays
        </li>
      </ul>
      <p className="field-hint">Classes : quantiles des valeurs rapportées cette année-là (couleur seulement ; la valeur exacte est dans le tableau).</p>
      <h3>Sources de cette carte</h3>
      <ul>
        {Object.entries(d.by_source).map(([s, n]) => (
          <li key={s}>
            <code>{s}</code> : {n} pays
          </li>
        ))}
      </ul>
      <p className="field-hint">
        Ordre déclaré (« meilleure disponible ») : {d.concept.source_order.join(' → ') || 'non déclaré'}. Aucun mélange, aucune imputation.
      </p>
      {flagged.length > 0 && (
        <p>
          <strong>Drapeaux du fournisseur</strong> sur {flagged.length} cellule(s) : {flagKinds.join(', ')}.
        </p>
      )}
      <p className="muted">
        Instantané atlas {data.snapshot.snapshot_sha256.slice(0, 12)}… · arrêté au {data.snapshot.knowledge_cutoff.slice(0, 10)}{' '}
        {data.snapshot.origin === 'test_fixture' && <FixtureBadge />}
      </p>
      {data.warnings.length > 0 && (
        <ul className="v3-warnings">
          {warningsFr(data.warnings).map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </aside>
  );
}

function Ranking({ data, nameOf }: { data: AtlasMapResponse; nameOf: (iso: string) => string }) {
  const rows = useMemo(() => ranking(data.data.values), [data]);
  const parent = useRef<HTMLDivElement>(null);
  const v = useVirtualizer({ count: rows.length, getScrollElement: () => parent.current, estimateSize: () => 40, overscan: 10 });
  const vi = v.getVirtualItems();
  const list = vi.length ? vi.map((r) => ({ i: r.index, start: r.start, key: r.key })) : rows.slice(0, 60).map((_, i) => ({ i, start: -1, key: i }));
  return (
    <section className="v3-ranking" aria-labelledby="ranking-title">
      <h2 id="ranking-title">
        Classement {data.data.year} ({rows.length} pays avec une valeur)
      </h2>
      <div className="v3-rank-head" aria-hidden="true">
        <span>#</span>
        <span>Pays</span>
        <span>Valeur</span>
        <span>Source</span>
      </div>
      <div ref={parent} className="v3-virtual-list v3-rank-list" role="table" aria-label={`Classement ${data.data.year}`} data-testid="atlas-ranking">
        <div role="rowgroup" style={{ height: vi.length ? v.getTotalSize() : undefined, position: 'relative' }}>
          {list.map((r) => {
            const c = rows[r.i]!;
            const flags = flagLines(c.flags);
            return (
              <div
                key={r.key}
                role="row"
                data-index={r.i}
                data-iso3={c.iso3}
                ref={vi.length ? v.measureElement : undefined}
                className="v3-rank-row"
                style={r.start >= 0 ? { position: 'absolute', top: 0, left: 0, right: 0, transform: `translateY(${r.start}px)` } : undefined}
              >
                <span role="cell">{r.i + 1}</span>
                <span role="cell">
                  <Link to={`/v3/pays/${c.iso3}`}>{nameOf(c.iso3!)}</Link> <span className="muted">{c.iso3}</span>
                </span>
                <span role="cell" title={c.value ?? c.value_text ?? ''} className="v3-num">
                  {cellText(c)}
                  {c.year !== null && c.year !== data.data.year ? <span className="muted"> ({c.year})</span> : null}
                </span>
                <span role="cell" className="v3-rank-src">
                  <CitationChip c={citeAtlasCell(c)} compact />
                  {flags.length > 0 && <span className="v3-flags">{flags.join(' · ')}</span>}
                </span>
              </div>
            );
          })}
        </div>
      </div>
      {data.data.countries_without_value.length > 0 && (
        <details className="v3-gaps" open={rows.length === 0}>
          <summary>Pays sans valeur en {data.data.year} ({data.data.countries_without_value.length}) — hachurés sur la carte</summary>
          <ul className="v3-gap-list" data-testid="atlas-gaps">
            {data.data.countries_without_value.map((iso) => (
              <li key={iso} data-gap-country={iso}>
                <Link to={`/v3/pays/${iso}`}>{nameOf(iso)}</Link> <MissingCell reason="no_declared_source" note={NO_SOURCE_REASON} />
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
