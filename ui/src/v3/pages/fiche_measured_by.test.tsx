/**
 * V6 INTEG: the Fiche resolves both measured_by conventions — a catalog concept (atlas:<concept>, V4CORE) and an
 * atlas table entity (atlas:<source>.<dataset>, V4ECON/V4MIND/V4ECO) — and shows a provider's required notice
 * (e.g. New York Fed terms) next to the values.
 *
 * TEST DATA ONLY: synthetic ids ("fixture:…"), illustrative values, not sourced; the notice text is synthetic.
 */
import { configure, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mockFetch } from "../../test/fetchMock";
import { AtlasHint } from "../api/families";
import { LicenceList } from "../components/Licence";
import { requiredNotices } from "../lib/licence";
import * as F from "../test/fixtures";
import V3App from "../V3App";

configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });
afterEach(() => vi.unstubAllGlobals());

const REG: [string, string] = ["fixture:region_fr101", "Région test"];
const TABLE: [string, string] = ["atlas:fixture_src.reg_gdp", "Table PIB régional (test)"];
const NOTICE = "FIXTURE notice: these synthetic data are subject to the provider's terms (test only).";
const OPEN = {
  licence: "CC-BY-4.0",
  licence_url: null,
  attribution: null,
  commercial_use: "permitted",
  non_commercial: false,
  share_alike: false,
  no_derivatives: false,
  open_licence: true,
  restricted: false,
  basis: "table_artifact",
  restriction: null,
  required_notice: null as string | null,
};
const WITH_NOTICE = { ...OPEN, required_notice: NOTICE };

const measured = F.stmt(REG, "econ:measured_by", "mesuré par", TABLE, { source: "fixture.econ" });
const neighbour = (atlas: Record<string, unknown>) => ({
  neighbour: { qid: TABLE[0], label: TABLE[1], kind: "data_series" },
  reading: { label: { fr: "est mesuré par", en: "is measured by" }, inverse: false },
  subtype: "atlas_table",
  mapping: { origin: "relations_v4.d/v4econ.yaml", agent: "V4ECON", direction: "forward" },
  share: null,
  atlas,
  rights: OPEN,
  statement: { ...measured, direction: "out" },
});
const families = (atlas: Record<string, unknown>) => ({
  qid: REG[0],
  label: REG[1],
  labels: { fr: REG[1] },
  kind: "nuts_region",
  families: [
    {
      family: "measured_by",
      label: { fr: "est mesuré par", en: "is measured by" },
      inverse_label: { fr: "mesure", en: "measures" },
      group: "data",
      symmetric: false,
      count: 1,
      count_is_lower_bound: false,
      shown: 1,
      truncated: false,
      properties: [],
      neighbours: [neighbour(atlas)],
      causal: false,
    },
  ],
  unmapped: [],
  literals: [],
  literals_truncated: false,
  scan_capped: false,
  registry: { families: 21, mappings: 137, files: ["relations_v4.yaml"] },
  notes: [],
});
const TABLE_HINT = {
  kind: "table",
  concept: null,
  table: "fixture_src.reg_gdp",
  source: "fixture_src",
  dataset: "reg_gdp",
  provider_geo: "FR101",
  iso3: null,
  indicators: [],
  indicator_contains: null,
};
const SNAP = { snapshot_sha256: "c".repeat(64), origin: "test_fixture", knowledge_cutoff: "2026-10-17T00:00:00Z", tables: 1, rows: 3 };
const regionCell = (year: number, value: string) => ({
  ...F.cell("FRA", year, value, { series: "fixture_src.reg_gdp.GDP|unit=EUR_HAB", unit: "EUR" }),
  iso3: null,
  provider_geo: "FR101",
  period_basis: "calendar_year",
  rights: WITH_NOTICE,
});
const tableBody = (inSnapshot: boolean) => ({
  data: {
    table: "fixture_src.reg_gdp",
    source: "fixture_src",
    dataset: "reg_gdp",
    in_snapshot: inSnapshot,
    filters: { provider_geo: "FR101" },
    concepts: [],
    indicators: inSnapshot
      ? [{ indicator: "GDP|unit=EUR_HAB", series: "fixture_src.reg_gdp.GDP|unit=EUR_HAB", cells: 2, values: 2, geographies: 1, countries: 0, period_first: "2021-01-01", period_last: "2023-01-01", unit: "EUR" }]
      : [],
    cells: inSnapshot ? [regionCell(2021, "41000"), regionCell(2022, "43000")] : [],
    truncated: false,
    licences: inSnapshot ? [{ source: "fixture_src", ...WITH_NOTICE }] : [],
  },
  snapshot: SNAP,
  notes: [],
  warnings: [],
});
const ENTITY = {
  qid: REG[0],
  snapshot: 1,
  kind: "nuts_region",
  labels: { fr: REG[1] },
  descriptions: {},
  aliases: {},
  sitelinks: null,
  provenance: null,
  statements_out: 1,
  statements_in: 0,
  degree_capped_at: 100000,
};
const PLACES = { qid: REG[0], label: REG[1], chain: [], levels: {}, country_iso3: null, coordinates: null, located_in_properties: [], note: "" };

function routes(atlas: Record<string, unknown>, extra: Record<string, { body: unknown }>) {
  const q = encodeURIComponent(REG[0]);
  return {
    [`GET /v1/kg/entities/${q}/families`]: { body: F.envelope(families(atlas)) },
    [`GET /v1/kg/entities/${q}/places`]: { body: F.envelope(PLACES) },
    [`GET /v1/kg/entities/${q}/timeline`]: { body: F.envelope({ qid: REG[0], label: REG[1], events: [], truncated: false }) },
    [`GET /v1/kg/entities/${q}`]: { body: F.envelope(ENTITY) },
    ...extra,
  };
}

function at(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/v3/*" element={<V3App />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("measured_by bridge (concept vs table entity)", () => {
  it("parses both hint kinds; an old concept hint without kind stays a concept", () => {
    expect(AtlasHint.parse(TABLE_HINT).kind).toBe("table");
    const old = AtlasHint.parse({ concept: "energy.oil_production", iso3: "SAU", table: null });
    expect([old.kind, old.concept, old.indicators]).toEqual(["concept", "energy.oil_production", []]);
  });

  it("reads a table entity with the statement's provider geography and shows the required notice", async () => {
    const m = mockFetch(routes(TABLE_HINT, { "GET /v1/atlas/tables/fixture_src/reg_gdp/cells": { body: tableBody(true) } }));
    at(`/v3/fiche?qid=${encodeURIComponent(REG[0])}`);
    const row = await screen.findByTestId("fiche-table-series");
    expect(row).toHaveTextContent("fixture_src.reg_gdp");
    expect(row).toHaveTextContent("code FR101");
    expect(row).toHaveTextContent("GDP|unit=EUR_HAB");
    expect(row).toHaveTextContent("2022-01-01 : 43000 EUR");
    expect(within(row).getByTestId("required-notices")).toHaveTextContent(NOTICE);
    const call = m.calls.find((c) => c.url.includes("/v1/atlas/tables/"));
    expect(call?.url).toContain("provider_geo=FR101");
    expect(m.calls.some((c) => c.url.includes("/v1/atlas/series"))).toBe(false);
  });

  it("says a table absent from the atlas snapshot is absent, not zero", async () => {
    mockFetch(routes(TABLE_HINT, { "GET /v1/atlas/tables/fixture_src/reg_gdp/cells": { body: tableBody(false) } }));
    at(`/v3/fiche?qid=${encodeURIComponent(REG[0])}`);
    expect(await screen.findByTestId("fiche-table-absent")).toHaveTextContent("absente, pas zéro");
  });
});

describe("required notices", () => {
  it("lists one verbatim notice per source, even under an open licence", () => {
    const ls = [
      { source: "fixture_src", ...WITH_NOTICE },
      { source: "fixture_src", ...WITH_NOTICE },
      { source: "open_src", ...OPEN },
    ];
    expect(requiredNotices(ls)).toEqual([{ source: "fixture_src", notice: NOTICE }]);
    render(<LicenceList licences={ls} />);
    expect(screen.getByTestId("required-notices")).toHaveTextContent(NOTICE);
    expect(screen.queryByTestId("licence-list")).toBeNull(); // nothing restricted: only the notice is shown
  });
});
