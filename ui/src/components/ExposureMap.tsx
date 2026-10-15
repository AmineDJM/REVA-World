import { useEffect, useMemo, useRef, useState } from 'react';
import { Map as MapLibreMap, setWorkerUrl, type GeoJSONSource, type MapMouseEvent, type StyleSpecification } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { FeatureCollection } from '../api/types';
import { prefersReducedMotion } from '../lib/useApi';

setWorkerUrl(workerUrl);

/** What a polygon shows. Only `value` carries a model result. */
export type FeatureState = 'value' | 'gap' | 'not_in_run' | 'no_entity';

export interface FeatureStyle {
  state: FeatureState;
  color?: string;
  /** Hover text (also used by the text alternative). */
  tooltip: string;
}

export interface MapArc {
  id: string;
  coordinates: [number, number][];
  width: number;
  label: string;
}

export interface ExposureMapProps {
  collection: FeatureCollection;
  /** Style per display feature id. */
  styles: Record<string, FeatureStyle>;
  arcs: MapArc[];
  /** Partner anchors drawn as small rings at the arc ends. */
  partnerPoints: { id: string; coordinates: [number, number]; label: string }[];
  selectedEntityId: string | null;
  onSelectEntity?: (entityId: string) => void;
  label: string;
  height?: number;
}

export const NOT_IN_RUN_COLOR = '#dfe3e8';
export const NO_ENTITY_COLOR = '#f1f3f5';
export const GAP_INK = '#6b4e16';
export const ARC_COLOR = '#8a5a00';

const BASE_STYLE: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#f6f8fa' } }],
};

/** A 12 px tile of 45° hatching for "no complete inputs" polygons (texture, not colour). */
function hatchImage(): { width: number; height: number; data: Uint8Array } {
  const size = 12;
  const data = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      const onLine = (x + y) % 6 === 0 || (x + y) % 6 === 1;
      // ink #6b4e16 on a pale paper so hatched countries never look like a value class
      const [r, g, b] = onLine ? [0x6b, 0x4e, 0x16] : [0xfb, 0xf7, 0xee];
      data[i] = r;
      data[i + 1] = g;
      data[i + 2] = b;
      data[i + 3] = 255;
    }
  }
  return { width: size, height: size, data };
}

interface Tip {
  x: number;
  y: number;
  text: string;
}

/**
 * MapLibre world map for one simulation run: choropleth from result rows, hatched gaps
 * with their typed reason on hover, and reported partner arcs for the selected country.
 * Display geometry only (Natural Earth, self-hosted GeoJSON from /v1/locations).
 */
export function ExposureMap({ collection, styles, arcs, partnerPoints, selectedEntityId, onSelectEntity, label, height = 420 }: ExposureMapProps) {
  const el = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const loaded = useRef(false);
  const onSelect = useRef(onSelectEntity);
  onSelect.current = onSelectEntity;
  const [failed, setFailed] = useState<string | null>(null);
  const [tip, setTip] = useState<Tip | null>(null);

  const featureData = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: collection.features.map((f) => {
        const st = styles[f.id] ?? { state: 'no_entity' as const, tooltip: f.properties.name ?? f.properties.feature_key };
        return {
          type: 'Feature' as const,
          geometry: f.geometry,
          properties: {
            fid: f.id,
            entity_id: f.properties.entity_id ?? '',
            state: st.state,
            color: st.color ?? (st.state === 'not_in_run' ? NOT_IN_RUN_COLOR : NO_ENTITY_COLOR),
            tooltip: st.tooltip,
            selected: !!selectedEntityId && f.properties.entity_id === selectedEntityId,
            disputed: f.properties.match_status === 'disputed',
          },
        };
      }),
    }),
    [collection, styles, selectedEntityId],
  );
  const arcData = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: arcs.map((a) => ({ type: 'Feature' as const, geometry: { type: 'LineString' as const, coordinates: a.coordinates }, properties: { id: a.id, width: a.width, label: a.label } })),
    }),
    [arcs],
  );
  const pointData = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: partnerPoints.map((p) => ({ type: 'Feature' as const, geometry: { type: 'Point' as const, coordinates: p.coordinates }, properties: { id: p.id, label: p.label } })),
    }),
    [partnerPoints],
  );
  const latest = useRef({ featureData, arcData, pointData });
  latest.current = { featureData, arcData, pointData };

  useEffect(() => {
    if (!el.current) return;
    let map: MapLibreMap;
    try {
      const reduced = prefersReducedMotion();
      map = new MapLibreMap({
        container: el.current,
        style: BASE_STYLE,
        center: [10, 25],
        zoom: 0.6,
        minZoom: 0,
        maxZoom: 6,
        renderWorldCopies: false,
        dragRotate: false,
        pitchWithRotate: false,
        touchPitch: false,
        fadeDuration: reduced ? 0 : 300,
        attributionControl: { compact: true, customAttribution: 'Display geometry: Made with Natural Earth' },
      });
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
      return;
    }
    mapRef.current = map;
    map.on('error', (e) => setFailed(e.error?.message ?? 'map error'));
    map.on('load', () => {
      const d = latest.current;
      map.addImage('hatch', hatchImage(), { pixelRatio: 2 });
      map.addSource('features', { type: 'geojson', data: d.featureData as never });
      map.addSource('arcs', { type: 'geojson', data: d.arcData as never });
      map.addSource('partners', { type: 'geojson', data: d.pointData as never });
      map.addLayer({
        id: 'fill',
        type: 'fill',
        source: 'features',
        filter: ['!=', ['get', 'state'], 'gap'],
        paint: { 'fill-color': ['get', 'color'] as never, 'fill-opacity': ['case', ['get', 'disputed'], 0.5, 0.95] as never },
      });
      map.addLayer({ id: 'gap', type: 'fill', source: 'features', filter: ['==', ['get', 'state'], 'gap'], paint: { 'fill-pattern': 'hatch' } });
      map.addLayer({
        id: 'outline',
        type: 'line',
        source: 'features',
        filter: ['!', ['get', 'disputed']],
        paint: { 'line-color': ['case', ['==', ['get', 'state'], 'gap'], GAP_INK, '#8c96a3'] as never, 'line-width': 0.5 },
      });
      map.addLayer({ id: 'disputed', type: 'line', source: 'features', filter: ['get', 'disputed'], paint: { 'line-color': '#7c2d12', 'line-width': 1.1, 'line-dasharray': [2, 2] } });
      map.addLayer({ id: 'selected', type: 'line', source: 'features', filter: ['get', 'selected'], paint: { 'line-color': '#111827', 'line-width': 2.4 } });
      map.addLayer({
        id: 'arcs',
        type: 'line',
        source: 'arcs',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': ARC_COLOR, 'line-opacity': 0.8, 'line-width': ['get', 'width'] as never },
      });
      map.addLayer({
        id: 'partners',
        type: 'circle',
        source: 'partners',
        paint: { 'circle-radius': 4, 'circle-color': '#ffffff', 'circle-stroke-color': ARC_COLOR, 'circle-stroke-width': 2 },
      });
      loaded.current = true;
    });
    const hover = (e: MapMouseEvent & { features?: unknown[] }) => {
      const f = map.queryRenderedFeatures(e.point, { layers: ['fill', 'gap'] })[0];
      if (!f) {
        setTip(null);
        map.getCanvas().style.cursor = '';
        return;
      }
      map.getCanvas().style.cursor = f.properties?.entity_id ? 'pointer' : '';
      setTip({ x: e.point.x, y: e.point.y, text: String(f.properties?.tooltip ?? '') });
    };
    map.on('mousemove', hover);
    map.on('mouseout', () => setTip(null));
    map.on('click', (e) => {
      const f = map.queryRenderedFeatures(e.point, { layers: ['fill', 'gap'] })[0];
      const id = f?.properties?.entity_id;
      if (id && onSelect.current) onSelect.current(String(id));
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
    (map.getSource('features') as GeoJSONSource | undefined)?.setData(featureData as never);
    (map.getSource('arcs') as GeoJSONSource | undefined)?.setData(arcData as never);
    (map.getSource('partners') as GeoJSONSource | undefined)?.setData(pointData as never);
  }, [featureData, arcData, pointData]);

  // Data that changes before the style loads is picked up by the load handler (latest.current).

  if (failed) {
    return (
      <div className="state state-error" role="status" style={{ minHeight: 120 }}>
        Map could not be displayed in this browser ({failed}). The country table below lists the same data.
      </div>
    );
  }
  return (
    <div className="exposure-map-wrap">
      <div ref={el} className="map" style={{ height }} role="img" aria-label={label} data-testid="exposure-map" />
      {tip && (
        <div className="map-tip" role="tooltip" style={{ left: tip.x, top: tip.y }} data-testid="map-tip">
          {tip.text}
        </div>
      )}
    </div>
  );
}
