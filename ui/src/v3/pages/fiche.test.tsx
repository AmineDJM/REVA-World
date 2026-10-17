import {
  configure,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockFetch } from "../../test/fetchMock";
import { EntityFamilies } from "../api/families";
import {
  licencesOf,
  literalText,
  project,
  shareFr,
  splitLiterals,
} from "../lib/families";
import * as F from "../test/fixtures";
import V3App from "../V3App";

configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });
afterEach(() => vi.unstubAllGlobals());

// TEST DATA ONLY (synthetic ids "fixture:…"; values illustrative, not sourced).
const PET: [string, string] = ["fixture:petroleum", "Pétrole"];
const ALK: [string, string] = ["fixture:alkane", "Alcane"];
const FIRM: [string, string] = ["fixture:firm", "Entreprise X"];
const COW = {
  licence: "COW terms of use (not an open licence)",
  licence_url: null,
  attribution: null,
  commercial_use: "restricted",
  non_commercial: false,
  share_alike: false,
  no_derivatives: false,
  open_licence: false,
  restricted: true,
  basis: "raw_artifact_rights",
};
const CC0 = {
  ...COW,
  licence: "CC0-1.0 (Wikidata)",
  commercial_use: "permitted",
  open_licence: true,
  restricted: false,
  basis: "wikidata_cc0",
};

const made = F.stmt(PET, "P186", "matériau", ALK);
const produced = F.stmt(FIRM, "P1056", "produit", PET, {
  source: "fixture.firms",
});
const casNo = F.stmt(PET, "P231", "numéro CAS", null, {
  value: { type: "external_id", value: "8002-05-9" },
});
const density = F.stmt(PET, "P2054", "masse volumique", null, {
  value: { type: "quantity", amount: "+0.85", unit: "g/cm3" },
});

const FAMILIES = {
  qid: PET[0],
  label: PET[1],
  labels: { fr: "Pétrole" },
  kind: "material",
  families: [
    {
      family: "made_of",
      label: { fr: "est composé de", en: "is made of" },
      inverse_label: { fr: "est un constituant de", en: "is a constituent of" },
      group: "structure",
      symmetric: false,
      count: 1,
      count_is_lower_bound: false,
      shown: 1,
      truncated: false,
      properties: [
        {
          pid: "P186",
          count: 1,
          label_fr: "matériau",
          label_en: "made from material",
        },
      ],
      neighbours: [
        {
          neighbour: { qid: ALK[0], label: ALK[1], kind: "compound" },
          reading: {
            label: { fr: "est composé de", en: "is made of" },
            inverse: false,
          },
          subtype: "made_from_material",
          mapping: {
            origin: "relations_v4.yaml",
            agent: "V4CORE",
            direction: "forward",
          },
          share: {
            value: "0.25",
            fraction: "0.25",
            unit: "1",
            scale: "fraction",
            qualifier: "P1107",
          },
          atlas: null,
          rights: CC0,
          statement: { ...made, direction: "out" },
        },
      ],
      causal: false,
    },
    {
      family: "produced_by",
      label: { fr: "est produit par", en: "is produced by" },
      inverse_label: { fr: "produit", en: "produces" },
      group: "economy",
      symmetric: false,
      count: 3,
      count_is_lower_bound: false,
      shown: 1,
      truncated: true,
      properties: [],
      neighbours: [
        {
          neighbour: { qid: FIRM[0], label: FIRM[1], kind: "company" },
          reading: {
            label: { fr: "est produit par", en: "is produced by" },
            inverse: false,
          },
          subtype: null,
          mapping: {
            origin: "relations_v4.d/v4industry.yaml",
            agent: "V4INDUSTRY",
            direction: "forward",
          },
          share: null,
          atlas: null,
          rights: COW,
          statement: { ...produced, direction: "in" },
        },
      ],
      causal: false,
    },
  ],
  unmapped: [
    {
      pid: "P828",
      direction: "in",
      count: 2,
      label_fr: "a pour cause",
      label_en: "has cause",
      causal_assertion: true,
    },
  ],
  literals: [
    { ...casNo, rights: CC0 },
    { ...density, rights: CC0 },
  ],
  literals_truncated: false,
  scan_capped: false,
  registry: { families: 21, mappings: 57, files: ["relations_v4.yaml"] },
  notes: [],
};

const PLACES = {
  qid: PET[0],
  label: PET[1],
  chain: [
    {
      entity: { qid: PET[0], label: PET[1], kind: "material" },
      level: null,
      via: null,
      statement_key: null,
      basis: "start",
      iso3: null,
      coordinates: null,
    },
    {
      entity: {
        qid: "iso3166_2:SA-04",
        label: "Province orientale",
        kind: "region_adm1",
      },
      level: null,
      via: "place:located_in",
      statement_key: "k1",
      basis: "statement",
      iso3: null,
      coordinates: {
        latitude: 25.0,
        longitude: 49.5,
        statement_key: "k2",
        source_key: "wikidata",
      },
    },
    {
      entity: { qid: "Q851", label: "Arabie saoudite", kind: "country" },
      level: null,
      via: null,
      statement_key: null,
      basis: "iso3166_2_code_prefix",
      iso3: "SAU",
      coordinates: null,
    },
  ],
  levels: {},
  country_iso3: "SAU",
  coordinates: null,
  located_in_properties: ["P131", "place:located_in"],
  note: "",
};

const ENTITY = {
  qid: PET[0],
  snapshot: 1,
  kind: "material",
  labels: { fr: "Pétrole", en: "Petroleum" },
  descriptions: {},
  aliases: {},
  sitelinks: null,
  provenance: null,
  statements_out: 3,
  statements_in: 1,
  degree_capped_at: 100000,
};

function at(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/v3/*" element={<V3App />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Fiche helpers", () => {
  it("formats shares, never inventing zero", () => {
    expect(shareFr(null)).toBeNull();
    expect(
      shareFr({
        value: "0.25",
        fraction: "0.25",
        unit: "1",
        scale: "fraction",
        qualifier: "P1107",
      }),
    ).toBe("25 %");
    expect(
      shareFr({
        value: "12",
        fraction: null,
        unit: "kg",
        scale: "source_unit",
        qualifier: "q",
      }),
    ).toBe("12 kg");
  });
  it("splits and renders literals", () => {
    const parts = splitLiterals([
      { value: { type: "external_id", value: "x" } },
      { value: { type: "quantity", amount: "+0.85", unit: "g/cm3" } },
      { value: { type: "globe_coordinate", latitude: 1, longitude: 2 } },
    ]);
    expect([
      parts.identifier.length,
      parts.value.length,
      parts.coordinate.length,
    ]).toEqual([1, 1, 1]);
    expect(
      literalText({ type: "quantity", amount: "+0.85", unit: "g/cm3" }),
    ).toBe("0,85 g/cm3");
    expect(
      literalText({
        type: "time",
        time: "1859-08-27T00:00:00Z",
        precision: 11,
      }),
    ).toBe("27 août 1859");
    expect(literalText(null)).toBe("valeur absente");
    expect(project(0, 0, 360, 180)).toEqual({ x: 180, y: 90 });
  });
  it("parses the families contract and lists every value licence", () => {
    const d = EntityFamilies.parse(FAMILIES);
    const ls = licencesOf(d.families, d.literals);
    expect(ls.filter((l) => l.restricted).map((l) => l.source)).toEqual([
      "fixture.firms",
    ]);
  });
});

describe("Fiche", () => {
  it("renders any entity: identifiers, values, families with share and licence, space, unmapped causal claims", async () => {
    mockFetch({
      "GET /v1/kg/entities/fixture%3Apetroleum/families": {
        body: F.envelope(FAMILIES),
      },
      "GET /v1/kg/entities/fixture%3Apetroleum/places": {
        body: F.envelope(PLACES),
      },
      "GET /v1/kg/entities/fixture%3Apetroleum/timeline": {
        body: F.envelope({
          qid: PET[0],
          label: PET[1],
          events: [],
          truncated: false,
        }),
      },
      "GET /v1/kg/entities/fixture%3Apetroleum": { body: F.envelope(ENTITY) },
    });
    at("/v3/fiche?qid=fixture:petroleum");
    expect(
      await screen.findByRole("heading", { name: /Fiche — Pétrole/ }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("families-not-cause")).toHaveTextContent(
      "Aucune n’est causale",
    );
    const made = await screen.findByTestId("family-made_of");
    expect(made).toHaveTextContent("est composé de");
    expect(made).toHaveTextContent("part : 25 %");
    expect(within(made).getByRole("link", { name: "Alcane" })).toHaveAttribute(
      "href",
      "/v3/fiche?qid=fixture%3Aalkane",
    );
    const prod = screen.getByTestId("family-produced_by");
    expect(prod).toHaveTextContent("1 affichée(s) sur 3");
    expect(within(prod).getByTestId("licence-badge")).toHaveTextContent(
      "accès payant interdit",
    );
    expect(screen.getByTestId("fiche-identifiers")).toHaveTextContent(
      "8002-05-9",
    );
    expect(screen.getByTestId("fiche-values")).toHaveTextContent("0,85 g/cm3");
    const places = await screen.findByTestId("fiche-places");
    expect(places).toHaveTextContent(
      "pays déduit du préfixe du code ISO 3166-2",
    );
    expect(
      within(places).getByRole("link", { name: "dossier SAU" }),
    ).toHaveAttribute("href", "/v3/pays/SAU");
    expect(screen.getByTestId("fiche-map")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("Province orientale"),
    );
    expect(screen.getByTestId("fiche-unmapped")).toHaveTextContent(
      "affirmation causale d’une source",
    );
    await waitFor(() =>
      expect(screen.getByTestId("licence-list")).toHaveTextContent(
        "fixture.firms",
      ),
    );
  });

  it("says the service is not available rather than showing anything", async () => {
    mockFetch({
      "GET /v1/kg/entities/fixture%3Apetroleum": { body: F.envelope(ENTITY) },
    });
    at("/v3/fiche?qid=fixture:petroleum");
    await waitFor(() =>
      expect(
        document.querySelectorAll("[data-v3-state]").length,
      ).toBeGreaterThan(0),
    );
    expect(screen.queryByTestId("family-made_of")).toBeNull();
  });
});
