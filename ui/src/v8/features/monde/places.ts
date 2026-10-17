import { useEffect, useState } from 'react';
import { getPlaces, type Place } from './api';

let displayNames: Intl.DisplayNames | null | undefined;
function regionNames(): Intl.DisplayNames | null {
  if (displayNames === undefined) {
    try {
      displayNames = new Intl.DisplayNames(['fr'], { type: 'region' });
    } catch {
      displayNames = null;
    }
  }
  return displayNames;
}

/** French name of a country: the crosswalk's French label, else the browser's region name (ISO2), else its label. */
export function frName(p: Place | undefined, fallback: string): string {
  if (!p) return fallback;
  if (p.label_fr) return p.label_fr;
  if (p.iso2) {
    try {
      const n = regionNames()?.of(p.iso2);
      if (n && n !== p.iso2) return n;
    } catch {
      /* unknown region code */
    }
  }
  return p.label;
}

/** iso3 → French name (loaded once per session from /v1/v8/series/places). */
export function usePlaceNames(): { nameOf: (iso3: string) => string; places: Place[] } {
  const [places, setPlaces] = useState<Place[]>([]);
  useEffect(() => {
    let live = true;
    getPlaces()
      .then((p) => live && setPlaces(p))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  const by = new Map(places.map((p) => [p.iso3, p]));
  return { nameOf: (iso3: string) => frName(by.get(iso3), iso3), places };
}
