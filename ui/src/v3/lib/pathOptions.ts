/** Advanced options of the Chemin screen (LINKS constrained path search: /v1/links/path). */

/** Non-transit presets: objects of these properties may end a path but are never passed through. */
export const NON_TRANSIT_PRESETS: { pid: string; label: string }[] = [
  { pid: 'P37', label: 'langue officielle' },
  { pid: 'P2936', label: 'langue parlée' },
  { pid: 'P30', label: 'continent' },
  { pid: 'P2348', label: 'période' },
];
export const KIND_RE = /^((statement|link):)?([A-Za-z][A-Za-z0-9_]{0,63}|[a-z][a-z0-9_]{0,31}:[a-z][a-z0-9_]{0,63})$/;
export const NODE_RE = /^(Q[1-9]\d{0,11}|[A-Z]{3}|[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:-]{1,128})$/;
export const PROP_RE = /^(P[1-9]\d{0,8}|[a-z][a-z0-9_]{0,31}:[a-z][a-z0-9_]{0,63})$/;

/** "P1891,P710 > P37" -> ["P1891,P710", "P37"]; null when a kind is malformed. */
export function parseSteps(text: string): string[] | null {
  const steps = text
    .split('>')
    .map((st) =>
      st
        .split(',')
        .map((x) => x.trim())
        .filter(Boolean),
    )
    .filter((st) => st.length > 0);
  if (steps.some((st) => st.some((k) => !KIND_RE.test(k)))) return null;
  return steps.map((st) => st.join(','));
}

export function parseList(text: string, re: RegExp): string[] | null {
  const items = text
    .split(/[\s,;]+/)
    .map((x) => x.trim())
    .filter(Boolean);
  return items.every((x) => re.test(x)) ? items : null;
}
