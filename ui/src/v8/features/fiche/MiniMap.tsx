/**
 * Mini-map of the Fiche: the country display outline (Natural Earth display layer, when the bundle carries it) and
 * the entity's point (sourced coordinates), equirectangular, framed on what is drawn. Display only: an outline is
 * not a boundary claim. No tiles, no network.
 */
interface Geo {
  type: string;
  coordinates: unknown;
}

type Ring = [number, number][];

function rings(g: Geo | null): Ring[] {
  if (!g) return [];
  if (g.type === "Polygon") return (g.coordinates as Ring[]) ?? [];
  if (g.type === "MultiPolygon")
    return ((g.coordinates as Ring[][]) ?? []).flat();
  return [];
}

export function MiniMap({
  geometry,
  point,
  label,
  width = 320,
  height = 200,
}: {
  geometry: Geo | null;
  point: { lat: number; lon: number } | null;
  label: string;
  width?: number;
  height?: number;
}) {
  const rs = rings(geometry);
  let pts: [number, number][] = rs.flat();
  // Frame on the outline; drop far-away overseas parts when a point exists inside the main part.
  if (point) pts = pts.length ? pts : [[point.lon, point.lat]];
  if (!pts.length) return null;
  const main = mainFrame(rs, point);
  let [x0, x1, y0, y1] = main ?? bbox(pts);
  if (point) {
    x0 = Math.min(x0, point.lon);
    x1 = Math.max(x1, point.lon);
    y0 = Math.min(y0, point.lat);
    y1 = Math.max(y1, point.lat);
  }
  const padX = Math.max((x1 - x0) * 0.08, 0.5);
  const padY = Math.max((y1 - y0) * 0.08, 0.5);
  x0 -= padX;
  x1 += padX;
  y0 -= padY;
  y1 += padY;
  const kx = Math.cos((((y0 + y1) / 2) * Math.PI) / 180) || 1;
  const sx = width / ((x1 - x0) * kx || 1);
  const sy = height / (y1 - y0 || 1);
  const s = Math.min(sx, sy);
  const ox = (width - (x1 - x0) * kx * s) / 2;
  const oy = (height - (y1 - y0) * s) / 2;
  const P = (lon: number, lat: number) =>
    [ox + (lon - x0) * kx * s, oy + (y1 - lat) * s] as const;
  const d = rs
    .map(
      (r) =>
        r
          .map(
            ([lon, lat], i) =>
              `${i ? "L" : "M"}${P(lon, lat)[0].toFixed(1)},${P(lon, lat)[1].toFixed(1)}`,
          )
          .join("") + "Z",
    )
    .join("");
  const pp = point ? P(point.lon, point.lat) : null;
  return (
    <svg
      className="fiche-map"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Carte : ${label}${point ? ` (${point.lat.toFixed(3)}°, ${point.lon.toFixed(3)}°)` : ""}`}
      data-testid="fiche-map"
      preserveAspectRatio="xMidYMid meet"
    >
      {d && <path d={d} className="fiche-map-shape" />}
      {pp && (
        <g className="fiche-map-pt">
          <circle cx={pp[0]} cy={pp[1]} r={9} className="fiche-map-halo" />
          <circle cx={pp[0]} cy={pp[1]} r={4} />
        </g>
      )}
    </svg>
  );
}

function bbox(pts: [number, number][]): [number, number, number, number] {
  let x0 = Infinity;
  let x1 = -Infinity;
  let y0 = Infinity;
  let y1 = -Infinity;
  for (const [x, y] of pts) {
    if (x < x0) x0 = x;
    if (x > x1) x1 = x;
    if (y < y0) y0 = y;
    if (y > y1) y1 = y;
  }
  return [x0, x1, y0, y1];
}

/** Bounding box of the largest ring (or the ring holding the point): France without French Guiana. */
function mainFrame(
  rs: Ring[],
  point: { lat: number; lon: number } | null,
): [number, number, number, number] | null {
  if (rs.length <= 1) return null;
  const boxes = rs.map((r) => bbox(r));
  let best = 0;
  let bestArea = -1;
  boxes.forEach((b, i) => {
    const inside =
      point &&
      point.lon >= b[0] &&
      point.lon <= b[1] &&
      point.lat >= b[2] &&
      point.lat <= b[3];
    const area = (b[1] - b[0]) * (b[3] - b[2]) + (inside ? 1e9 : 0);
    if (area > bestArea) {
      bestArea = area;
      best = i;
    }
  });
  return boxes[best] ?? null;
}
