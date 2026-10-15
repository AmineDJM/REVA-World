import { useEffect, useMemo, useRef, useState } from 'react';
import { Map as MapLibreMap, setWorkerUrl, type GeoJSONSource, type MapMouseEvent, type StyleSpecification } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { FeatureCollection } from '../../api/types';
import { prefersReducedMotion } from '../../lib/useApi';
import { GAP_INK, iso3OfFeatureKey, OUTSIDE, tooltipFor, type CountryState } from '../lib/choropleth';

setWorkerUrl(workerUrl);

const BASE_STYLE: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#eaf1f8' } }],
};

/** 45° hatching tile for gaps (texture, never a value colour). */
function hatch(): { width: number; height: number; data: Uint8Array } {
  const size = 12;
  const data = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++)
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      const on = (x + y) % 6 === 0 || (x + y) % 6 === 1;
      const [r, g, b] = on ? [0x6b, 0x4e, 0x16] : [0xfb, 0xf7, 0xee];
      data[i] = r;
      data[i + 1] = g;
      data[i + 2] = b;
      data[i + 3] = 255;
    }
  return { width: size, height: size, data };
}

export interface AtlasMapProps {
  collection: FeatureCollection;
  states: Map<string, CountryState>;
  year: number;
  selected: string | null;
  onSelect: (iso3: string) => void;
  label: string;
  height?: number;
}

/**
 * MapLibre choropleth for one atlas concept and year: coloured from reported cells only, gaps
 * hatched with their reason on hover, polygons outside the referential in light grey.
 * Display geometry: Natural Earth via /v1/locations (self-hosted, no tile service).
 */
export default function AtlasMap({ collection, states, year, selected, onSelect, label, height = 460 }: AtlasMapProps) {
  const el = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const loaded = useRef(false);
  const select = useRef(onSelect);
  select.current = onSelect;
  const [failed, setFailed] = useState<string | null>(null);
  const [tip, setTip] = useState<{ x: number; y: number; text: string } | null>(null);

  const data = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: collection.features.map((f) => {
        const iso = iso3OfFeatureKey(f.properties.feature_key);
        const st = iso ? states.get(iso) : undefined;
        const name = f.properties.name ?? f.properties.entity_label ?? iso ?? f.properties.feature_key;
        return {
          type: 'Feature' as const,
          geometry: f.geometry,
          properties: {
            iso3: iso ?? '',
            state: st?.state ?? 'outside',
            color: st?.state === 'value' ? st.color : OUTSIDE,
            tooltip: tooltipFor(name, st, year),
            selected: !!selected && iso === selected,
            disputed: f.properties.match_status === 'disputed',
          },
        };
      }),
    }),
    [collection, states, year, selected],
  );
  const latest = useRef(data);
  latest.current = data;

  useEffect(() => {
    if (!el.current) return;
    let map: MapLibreMap;
    try {
      map = new MapLibreMap({
        container: el.current,
        style: BASE_STYLE,
        center: [10, 20],
        zoom: 0.7,
        minZoom: 0,
        maxZoom: 6,
        renderWorldCopies: false,
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        fadeDuration: prefersReducedMotion() ? 0 : 200,
        attributionControl: { compact: true, customAttribution: 'Géométrie d’affichage : Made with Natural Earth' },
      });
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
      return;
    }
    mapRef.current = map;
    map.on('error', (e) => setFailed(e.error?.message ?? 'erreur de carte'));
    map.on('load', () => {
      map.addImage('v3-hatch', hatch(), { pixelRatio: 2 });
      map.addSource('countries', { type: 'geojson', data: latest.current as never });
      map.addLayer({ id: 'fill', type: 'fill', source: 'countries', filter: ['!=', ['get', 'state'], 'gap'], paint: { 'fill-color': ['get', 'color'] as never, 'fill-opacity': ['case', ['get', 'disputed'], 0.5, 0.95] as never } });
      map.addLayer({ id: 'gap', type: 'fill', source: 'countries', filter: ['==', ['get', 'state'], 'gap'], paint: { 'fill-pattern': 'v3-hatch' } });
      map.addLayer({ id: 'outline', type: 'line', source: 'countries', paint: { 'line-color': ['case', ['==', ['get', 'state'], 'gap'], GAP_INK, '#8c96a3'] as never, 'line-width': 0.5 } });
      map.addLayer({ id: 'selected', type: 'line', source: 'countries', filter: ['get', 'selected'], paint: { 'line-color': '#111827', 'line-width': 2.4 } });
      loaded.current = true;
    });
    const at = (e: MapMouseEvent) => map.queryRenderedFeatures(e.point, { layers: ['fill', 'gap'] })[0];
    map.on('mousemove', (e) => {
      const f = at(e);
      map.getCanvas().style.cursor = f?.properties?.iso3 ? 'pointer' : '';
      setTip(f ? { x: e.point.x, y: e.point.y, text: String(f.properties?.tooltip ?? '') } : null);
    });
    map.on('mouseout', () => setTip(null));
    map.on('click', (e) => {
      const iso = at(e)?.properties?.iso3;
      if (iso) select.current(String(iso));
    });
    return () => {
      loaded.current = false;
      mapRef.current = null;
      map.remove();
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded.current) return;
    (map.getSource('countries') as GeoJSONSource | undefined)?.setData(data as never);
  }, [data]);

  if (failed) {
    return (
      <div className="state state-error" role="status">
        La carte ne peut pas s’afficher dans ce navigateur ({failed}). Le classement ci-dessous contient les mêmes données.
      </div>
    );
  }
  return (
    <div className="v3-map-wrap">
      <div ref={el} className="map v3-map" style={{ height }} role="img" aria-label={label} data-testid="atlas-map" />
      {tip && (
        <div className="map-tip" role="tooltip" style={{ left: tip.x, top: tip.y }} data-testid="atlas-map-tip">
          {tip.text}
        </div>
      )}
    </div>
  );
}
