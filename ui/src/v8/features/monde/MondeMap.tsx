import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Map as MapLibreMap, setWorkerUrl, type GeoJSONSource, type MapMouseEvent, type StyleSpecification } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { FeatureCollection } from '../../../api/types';
import { iso3OfFeatureKey } from '../../../v3/lib/choropleth';

setWorkerUrl(workerUrl);

/** What one country shows: a value class colour, a projection (colour + dotted overlay) or a hatched gap. */
export type Fill = { state: 'value'; color: string } | { state: 'proj'; color: string } | { state: 'missing' };

export interface MondeMapProps {
  collection: FeatureCollection;
  fills: Map<string, Fill>;
  selected: string | null;
  /** Places highlighted from elsewhere (scatter hover). */
  highlight?: string | null;
  dark: boolean;
  label: string;
  onSelect: (iso3: string) => void;
  renderTip: (iso3: string, name: string) => ReactNode;
  height?: number | string;
}

function cssVar(name: string, fallback: string): string {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch {
    return fallback;
  }
}

function parseColor(c: string): [number, number, number, number] {
  const m = /rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+))?\)/.exec(c);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3]), Math.round(255 * (m[4] === undefined ? 1 : Number(m[4])))];
  const h = /^#([0-9a-f]{6})$/i.exec(c);
  if (h) {
    const n = parseInt(h[1]!, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 255];
  }
  return [128, 128, 128, 255];
}

/** 45° hatch tile (missing: texture, never a value colour). */
function hatchTile(stripe: string, bg: string) {
  const size = 12;
  const data = new Uint8Array(size * size * 4);
  const s = parseColor(stripe);
  const b = parseColor(bg);
  for (let y = 0; y < size; y++)
    for (let x = 0; x < size; x++) {
      const on = (x + y) % 6 === 0 || (x + y) % 6 === 1;
      const a = on ? s[3] / 255 : 0;
      const i = (y * size + x) * 4;
      data[i] = Math.round(s[0] * a + b[0] * (1 - a));
      data[i + 1] = Math.round(s[1] * a + b[1] * (1 - a));
      data[i + 2] = Math.round(s[2] * a + b[2] * (1 - a));
      data[i + 3] = 255;
    }
  return { width: size, height: size, data };
}

/** Sparse dots (projection overlay on top of the class colour). */
function dotTile(ink: string) {
  const size = 8;
  const data = new Uint8Array(size * size * 4);
  const c = parseColor(ink);
  for (const [x, y] of [
    [1, 1],
    [2, 1],
    [1, 2],
    [2, 2],
    [5, 5],
    [6, 5],
    [5, 6],
    [6, 6],
  ] as const) {
    const i = (y * size + x) * 4;
    data[i] = c[0];
    data[i + 1] = c[1];
    data[i + 2] = c[2];
    data[i + 3] = 230;
  }
  return { width: size, height: size, data };
}

const STYLE: StyleSpecification = { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#eef1f5' } }] };

/**
 * MapLibre choropleth (display geometry: Natural Earth via /v1/locations, self-hosted, no tile service).
 * Coloured only from returned cells; projections dotted on top of their class colour; gaps hatched with their
 * reason in the tooltip; polygons outside the referential in a plain neutral.
 */
export default function MondeMap({ collection, fills, selected, highlight = null, dark, label, onSelect, renderTip, height = 460 }: MondeMapProps) {
  const el = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [tip, setTip] = useState<{ x: number; y: number; iso3: string; name: string } | null>(null);
  const select = useRef(onSelect);
  select.current = onSelect;

  const data = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: collection.features.map((f) => {
        const iso = iso3OfFeatureKey(f.properties.feature_key);
        const fill = iso ? fills.get(iso) : undefined;
        return {
          type: 'Feature' as const,
          geometry: f.geometry,
          properties: {
            iso3: iso ?? '',
            name: f.properties.name ?? f.properties.entity_label ?? iso ?? '',
            state: fill?.state ?? (iso ? 'missing' : 'outside'),
            color: fill && fill.state !== 'missing' ? fill.color : '',
            selected: !!selected && iso === selected,
            highlight: !!highlight && iso === highlight,
          },
        };
      }),
    }),
    [collection, fills, selected, highlight],
  );
  const latest = useRef(data);
  latest.current = data;

  useEffect(() => {
    if (!el.current) return;
    let map: MapLibreMap;
    try {
      map = new MapLibreMap({
        container: el.current,
        style: STYLE,
        center: [12, 22],
        zoom: 0.75,
        minZoom: 0,
        maxZoom: 6,
        renderWorldCopies: false,
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        fadeDuration: 0,
        attributionControl: { compact: true, customAttribution: 'Géométrie d’affichage : Made with Natural Earth' },
      });
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
      return;
    }
    mapRef.current = map;
    map.on('error', (e) => setFailed(e.error?.message ?? 'erreur de carte'));
    map.on('load', () => {
      map.addSource('c', { type: 'geojson', data: latest.current as never });
      map.addLayer({ id: 'fill', type: 'fill', source: 'c', filter: ['in', ['get', 'state'], ['literal', ['value', 'proj']]], paint: { 'fill-color': ['get', 'color'] as never } });
      map.addLayer({ id: 'outside', type: 'fill', source: 'c', filter: ['==', ['get', 'state'], 'outside'], paint: { 'fill-color': '#dde1e6' } });
      map.addLayer({ id: 'gap', type: 'fill', source: 'c', filter: ['==', ['get', 'state'], 'missing'], paint: { 'fill-color': '#faf5ea' } });
      map.addLayer({ id: 'projdots', type: 'fill', source: 'c', filter: ['==', ['get', 'state'], 'proj'], paint: { 'fill-color': 'rgba(0,0,0,0)' } });
      map.addLayer({ id: 'line', type: 'line', source: 'c', paint: { 'line-color': '#8c96a3', 'line-width': 0.4 } });
      map.addLayer({ id: 'hl', type: 'line', source: 'c', filter: ['get', 'highlight'], paint: { 'line-color': '#e66767', 'line-width': 2 } });
      map.addLayer({ id: 'sel', type: 'line', source: 'c', filter: ['get', 'selected'], paint: { 'line-color': '#111827', 'line-width': 2.4 } });
      setReady(true);
    });
    const at = (e: MapMouseEvent) => map.queryRenderedFeatures(e.point, { layers: ['fill', 'gap', 'projdots'] })[0];
    map.on('mousemove', (e) => {
      const f = at(e);
      const iso = f?.properties?.iso3 ? String(f.properties.iso3) : '';
      map.getCanvas().style.cursor = iso ? 'pointer' : '';
      setTip(iso ? { x: e.point.x, y: e.point.y, iso3: iso, name: String(f?.properties?.name ?? iso) } : null);
    });
    map.on('mouseout', () => setTip(null));
    map.on('click', (e) => {
      const iso = at(e)?.properties?.iso3;
      if (iso) select.current(String(iso));
    });
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => map.resize()) : null;
    ro?.observe(el.current);
    return () => {
      ro?.disconnect();
      mapRef.current = null;
      setReady(false);
      map.remove();
    };
  }, []);

  // theme: patterns and neutral colours are read from the tokens at draw time
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const missBg = cssVar('--v8-missing-bg', dark ? '#2a2416' : '#faf5ea');
    const stripe = cssVar('--v8-missing-stripe', dark ? 'rgba(215,179,106,0.3)' : 'rgba(122,90,28,0.32)');
    const proj = cssVar('--v8-projection', dark ? '#b98cf5' : '#7c3fc4');
    for (const id of ['v8-hatch', 'v8-dots']) if (map.hasImage(id)) map.removeImage(id);
    map.addImage('v8-hatch', hatchTile(stripe, missBg), { pixelRatio: 2 });
    map.addImage('v8-dots', dotTile(dark ? '#ffffff' : '#15171c'), { pixelRatio: 2 });
    map.setPaintProperty('gap', 'fill-pattern', 'v8-hatch');
    map.setPaintProperty('projdots', 'fill-pattern', 'v8-dots');
    map.setPaintProperty('bg', 'background-color', dark ? '#0b0e12' : '#eef2f6');
    map.setPaintProperty('outside', 'fill-color', dark ? '#262b33' : '#dde1e6');
    map.setPaintProperty('line', 'line-color', dark ? '#3a414c' : '#9aa3ae');
    map.setPaintProperty('sel', 'line-color', cssVar('--v8-fg', dark ? '#e7e9ec' : '#15171c'));
    map.setPaintProperty('hl', 'line-color', proj);
  }, [dark, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    (map.getSource('c') as GeoJSONSource | undefined)?.setData(data as never);
  }, [data, ready]);

  if (failed) {
    return (
      <div className="v8m-map-fail" role="status">
        La carte ne peut pas s’afficher dans ce navigateur ({failed}). Le classement contient les mêmes données.
      </div>
    );
  }
  return (
    <div className="v8m-map-wrap" style={{ height }}>
      <div ref={el} className="v8m-map" role="img" aria-label={label} data-testid="monde-map" />
      {tip && (
        <div className="v8m-tip" role="tooltip" style={{ left: tip.x, top: tip.y }} data-testid="monde-tip">
          {renderTip(tip.iso3, tip.name)}
        </div>
      )}
    </div>
  );
}
