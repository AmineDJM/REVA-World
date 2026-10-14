import { useEffect, useRef, useState } from 'react';
import { Map as MapLibreMap, setWorkerUrl, type StyleSpecification } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { FeatureCollection } from '../api/types';
import { prefersReducedMotion } from '../lib/useApi';

setWorkerUrl(workerUrl);

/**
 * Data-availability classes. These describe how much catalogue-required data
 * REVA holds for a display feature. They say nothing about truth or quality.
 */
export type AvailabilityClass = 'complete' | 'partial' | 'none_available' | 'not_in_catalogue' | 'no_entity';

export const AVAILABILITY_LEGEND: { cls: AvailabilityClass; label: string; color: string }[] = [
  { cls: 'complete', label: 'All catalogue-required cells available', color: '#2b5c8a' },
  { cls: 'partial', label: 'Some catalogue-required cells available', color: '#7fa7cc' },
  { cls: 'none_available', label: 'In catalogue, no cells available', color: '#e3d9c6' },
  { cls: 'not_in_catalogue', label: 'Matched entity outside the V1 catalogue', color: '#d4d4d4' },
  { cls: 'no_entity', label: 'Display feature without a reviewed entity match (light outline)', color: '#efefef' },
];

const COLOR = Object.fromEntries(AVAILABILITY_LEGEND.map((l) => [l.cls, l.color])) as Record<AvailabilityClass, string>;

export interface MapViewProps {
  collection: FeatureCollection;
  /** Availability class by entity id; unmatched features use `no_entity`. */
  availability: Record<string, AvailabilityClass>;
  /** Optional entity to highlight (e.g. the country being viewed). */
  focusEntityId?: string | null;
  label: string;
  height?: number;
}

const BASE_STYLE: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#f6f8fa' } }],
};

/**
 * Self-hosted low-resolution map: GeoJSON from /v1/locations on a plain
 * background. No external tile service. Display geometry only.
 */
export function MapView({ collection, availability, focusEntityId, label, height = 380 }: MapViewProps) {
  const el = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    if (!el.current) return;
    let map: MapLibreMap | null = null;
    const data = {
      type: 'FeatureCollection' as const,
      features: collection.features.map((f) => {
        const eid = f.properties.entity_id ?? null;
        const cls: AvailabilityClass = eid ? (availability[eid] ?? 'not_in_catalogue') : 'no_entity';
        return {
          type: 'Feature' as const,
          id: undefined,
          geometry: f.geometry,
          properties: {
            match_status: f.properties.match_status,
            availability: cls,
            focus: !!focusEntityId && eid === focusEntityId,
            name: f.properties.name ?? f.properties.entity_label ?? f.properties.feature_key,
          },
        };
      }),
    };
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
      map.on('error', (e) => setFailed(e.error?.message ?? 'map error'));
      map.on('load', () => {
        if (!map) return;
        map.addSource('features', { type: 'geojson', data: data as never });
        map.addLayer({
          id: 'fill',
          type: 'fill',
          source: 'features',
          paint: {
            'fill-color': ['match', ['get', 'availability'], ...AVAILABILITY_LEGEND.flatMap((l) => [l.cls, l.color]), COLOR.no_entity] as never,
            'fill-opacity': ['case', ['==', ['get', 'match_status'], 'disputed'], 0.45, 0.9],
          },
        });
        map.addLayer({
          id: 'outline',
          type: 'line',
          source: 'features',
          filter: ['!=', ['get', 'match_status'], 'disputed'],
          paint: {
            'line-color': ['case', ['==', ['get', 'match_status'], 'unmatched'], '#b8bec6', '#6b7280'],
            'line-width': ['case', ['==', ['get', 'match_status'], 'unmatched'], 0.4, 0.7],
          },
        });
        // Disputed features: dashed dark outline, never a solid border.
        map.addLayer({
          id: 'disputed',
          type: 'line',
          source: 'features',
          filter: ['==', ['get', 'match_status'], 'disputed'],
          paint: { 'line-color': '#7c2d12', 'line-width': 1.2, 'line-dasharray': [2, 2] },
        });
        map.addLayer({
          id: 'focus',
          type: 'line',
          source: 'features',
          filter: ['==', ['get', 'focus'], true],
          paint: { 'line-color': '#111827', 'line-width': 2.5 },
        });
        if (focusEntityId) {
          const coords: [number, number][] = [];
          for (const f of collection.features) {
            if (f.properties.entity_id !== focusEntityId) continue;
            collectCoords(f.geometry.coordinates, coords);
          }
          if (coords.length) {
            const lons = coords.map((c) => c[0]);
            const lats = coords.map((c) => c[1]);
            map.fitBounds(
              [
                [Math.max(-180, Math.min(...lons)), Math.max(-85, Math.min(...lats))],
                [Math.min(180, Math.max(...lons)), Math.min(85, Math.max(...lats))],
              ],
              { padding: 20, animate: false, maxZoom: 4 },
            );
          }
        }
      });
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    }
    return () => {
      map?.remove();
    };
  }, [collection, availability, focusEntityId]);

  return (
    <figure className="map-figure">
      {failed ? (
        <div className="state state-error" role="status" style={{ height }}>
          Map could not be displayed in this browser ({failed}). The feature table below lists the same data.
        </div>
      ) : (
        <div ref={el} className="map" style={{ height }} role="img" aria-label={label} />
      )}
      <figcaption>
        <ul className="legend" aria-label="Map legend">
          {AVAILABILITY_LEGEND.map((l) => (
            <li key={l.cls}>
              <span className="swatch" style={{ background: l.color }} aria-hidden="true" /> {l.label}
            </li>
          ))}
          <li>
            <span className="swatch swatch-dashed" aria-hidden="true" /> Disputed display feature (dashed outline)
          </li>
        </ul>
        <p className="map-note">
          Colours show data availability in REVA only, not accuracy or truth. Natural Earth boundaries are display-only:
          they are not authoritative and do not resolve disputed boundaries. A statistical reporter territory can differ
          from the display polygon.
        </p>
      </figcaption>
    </figure>
  );
}

function collectCoords(c: unknown, out: [number, number][]) {
  if (!Array.isArray(c)) return;
  if (c.length >= 2 && typeof c[0] === 'number' && typeof c[1] === 'number') {
    out.push([c[0], c[1]]);
    return;
  }
  for (const x of c) collectCoords(x, out);
}
