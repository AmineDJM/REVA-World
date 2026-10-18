/**
 * Fiche — the universal entity page (owner V8FICHE): /v8/fiche/:kind/:id for any record of any kind (country by
 * ISO3, compound, gene, disease, drug, company, facility, conflict, taxon…), anchor pages (commodity/crude_oil),
 * forecast questions, effect estimates and sources. Reached from search and from every link (v8Links.entity).
 */
import { lazy } from "react";
import { v8Links, type PaletteCommand, type V8Feature } from "../../contract";

/** "fiche FRA", "fiche chebi:27732", "fiche Q142", "fiche pétrole brut" → the Fiche URL (null when not an id). */
export function ficheUrlFor(query: string): string | null {
  const q = query
    .trim()
    .replace(/^fiche\s+/i, "")
    .trim();
  if (!q) return null;
  if (/^[A-Za-z]{3}$/.test(q))
    return v8Links.entity("country", q.toUpperCase());
  if (/^Q[1-9]\d*$/.test(q)) return v8Links.entity("entity", q);
  if (/^fq_[0-9a-f]{24}$/.test(q)) return v8Links.entity("question", q);
  const m = /^([A-Za-z][A-Za-z0-9_]*):(.+)$/.exec(q);
  if (m) return v8Links.entity("entity", `${m[1]!.toLowerCase()}:${m[2]!}`);
  if (/^(p[ée]trole( brut)?|brent|crude oil|wti)$/i.test(q))
    return v8Links.entity("commodity", "crude_oil");
  return null;
}

const commands: PaletteCommand[] = [
  {
    id: "fiche.open",
    label: "Ouvrir une fiche (ISO3, Q-id, chebi:…, hgnc:…, chembl:…, lei:…)",
    keywords: ["fiche", "entité", "ouvrir", "pays", "identifiant"],
    run: (q) => ficheUrlFor(q),
  },
  {
    id: "fiche.crude_oil",
    label: "Fiche : pétrole brut (Brent)",
    keywords: ["pétrole", "brent", "brut", "oil", "matière première"],
    run: () => v8Links.entity("commodity", "crude_oil"),
  },
];

export const feature: V8Feature = {
  id: "fiche",
  title: "Fiche",
  path: "fiche/:kind/:id",
  order: 15,
  navHidden: true,
  icon: "◧",
  element: lazy(() => import("./FichePage")),
  commands,
};
