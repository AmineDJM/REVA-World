/**
 * Pure helpers of the "Fiche" screen (V4 relation families). Missing stays missing: a share or value the source
 * does not give is `null`, never 0.
 */
import type { AtlasLicence } from "../api/atlas";
import type {
  FamilyGroup,
  FamilyShare,
  LiteralStatement,
} from "../api/families";
import { frDecimal } from "./format";
import { formatKgTime } from "./time";

/** "25 %" for a dimensionless share, else the value as the source gives it with its unit; null = not given. */
export function shareFr(share: FamilyShare | null | undefined): string | null {
  if (!share) return null;
  if (share.fraction !== null) {
    const pct = Number(share.fraction) * 100;
    if (Number.isFinite(pct))
      return `${pct.toLocaleString("fr-FR", { maximumSignificantDigits: 4 })} %`;
  }
  return `${frDecimal(share.value)}${share.unit && share.unit !== "1" ? ` ${unitShort(share.unit)}` : ""}`;
}

function unitShort(u: string): string {
  const m = /\/entity\/(Q\d+)$/.exec(u);
  return m ? m[1]! : u;
}

export type LiteralKind = "identifier" | "coordinate" | "value";

export function literalKind(st: Pick<LiteralStatement, "value">): LiteralKind {
  const t = String(st.value?.type ?? "");
  if (t === "external_id") return "identifier";
  if (t === "globe_coordinate") return "coordinate";
  return "value";
}

/** Identifiers, coordinates and other values, each in source order. */
export function splitLiterals<T extends Pick<LiteralStatement, "value">>(
  ls: readonly T[],
): Record<LiteralKind, T[]> {
  const out: Record<LiteralKind, T[]> = {
    identifier: [],
    coordinate: [],
    value: [],
  };
  for (const l of ls) out[literalKind(l)].push(l);
  return out;
}

/** Text of a typed literal as the source gives it (quantities as decimal strings, times with their precision). */
export function literalText(
  value: Record<string, unknown> | null | undefined,
): string {
  if (!value) return "valeur absente";
  const v = value as Record<string, unknown>;
  switch (v.type) {
    case "quantity": {
      const amt =
        typeof v.amount === "string"
          ? frDecimal(v.amount)
          : String(v.amount ?? "");
      const unit =
        typeof v.unit === "string" && v.unit !== "1"
          ? ` ${unitShort(v.unit)}`
          : "";
      return `${amt}${unit}`;
    }
    case "time":
      return typeof v.time === "string" && typeof v.precision === "number"
        ? formatKgTime({ time: v.time, precision: v.precision })
        : String(v.time ?? "");
    case "globe_coordinate":
      return `${Number(v.latitude).toFixed(4)}°, ${Number(v.longitude).toFixed(4)}°`;
    case "monolingualtext":
      return `${String(v.text ?? v.value ?? "")}${v.language ? ` (${String(v.language)})` : ""}`;
    case "somevalue":
      return "valeur inconnue (selon la source)";
    case "novalue":
      return "aucune valeur (selon la source)";
    default:
      return String(v.value ?? v.text ?? v.raw ?? JSON.stringify(v));
  }
}

/** Equirectangular projection of a point into a width × height box (longitude −180..180, latitude 90..−90). */
export function project(
  lat: number,
  lon: number,
  width: number,
  height: number,
): { x: number; y: number } {
  return { x: ((lon + 180) / 360) * width, y: ((90 - lat) / 180) * height };
}

const LEVELS: Record<number, string> = {
  0: "pays",
  1: "division de 1er niveau (ADM1)",
  2: "division de 2e niveau (ADM2)",
  3: "ADM3",
};
export const levelFr = (level: number | null): string | null =>
  level === null ? null : (LEVELS[level] ?? `ADM${level}`);

/** Licences of every value shown (one entry per source and licence), for the restricted-licence list. */
export function licencesOf(
  groups: readonly FamilyGroup[],
  literals: readonly LiteralStatement[],
): AtlasLicence[] {
  const out: AtlasLicence[] = [];
  const add = (source: string, r: (typeof literals)[number]["rights"]) => {
    if (r) out.push({ ...r, source });
  };
  for (const g of groups)
    for (const n of g.neighbours)
      add(n.statement.provenance.source_key, n.rights);
  for (const l of literals) add(l.provenance.source_key, l.rights);
  return out;
}

export const FAMILY_NOT_CAUSE =
  "Les familles de relations regroupent des déclarations sourcées (fait partie de, est composé de, est situé dans, est produit par…). Aucune n’est causale : une relation n’est pas une explication. Les affirmations causales sont dans « Causes ».";
