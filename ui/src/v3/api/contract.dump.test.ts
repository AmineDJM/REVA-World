/// <reference types="node" />
/**
 * Contract check of the V3 zod schemas against REAL responses of the backend routes.
 *
 * Skipped unless UI3_CONTRACT_DUMP points at a JSON-lines file of captured responses
 * ({method, url, status, body} per line). To capture them from the backend integration tests:
 *
 *   UI3_DUMP=/tmp/v3dump.jsonl PYTHONPATH=ui/src/v3/test uv run pytest -p dumpresp \
 *     tests/integration/test_kg_api.py tests/integration/test_atlas_api.py \
 *     tests/integration/test_links_db.py tests/integration/test_nlqa_api.py
 *   UI3_CONTRACT_DUMP=/tmp/v3dump.jsonl npx vitest run src/v3/api/contract.dump.test.ts
 *
 * (ui/src/v3/test/dumpresp.py wraps starlette.testclient.TestClient.request and appends JSON bodies of
 * /v1/{kg,atlas,links,ask} 2xx responses.)
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { z } from 'zod';
import { AtlasConceptList, AtlasCoverageResponse, AtlasDossierResponse, AtlasMapResponse, AtlasSeriesResponse } from './atlas';
import { AskResponse } from './ask';
import { issuesOf } from './common';
import { KgEntityResponse, KgNeighboursResponse, KgPathResponse, KgSearchResponse, KgTimelineResponse } from './kg';
import { CountryLinksResponse, LinksPathResponse } from './links';

const FILE = process.env.UI3_CONTRACT_DUMP;

const ROUTES: [RegExp, z.ZodType][] = [
  [/^\/v1\/kg\/entities\/[^/]+\/neighbours/, KgNeighboursResponse],
  [/^\/v1\/kg\/entities\/[^/]+\/timeline/, KgTimelineResponse],
  [/^\/v1\/kg\/entities\/[^/?]+(\?|$)/, KgEntityResponse],
  [/^\/v1\/kg\/search/, KgSearchResponse],
  [/^\/v1\/kg\/path/, KgPathResponse],
  [/^\/v1\/atlas\/concepts/, AtlasConceptList],
  [/^\/v1\/atlas\/countries\//, AtlasDossierResponse],
  [/^\/v1\/atlas\/map/, AtlasMapResponse],
  [/^\/v1\/atlas\/series/, AtlasSeriesResponse],
  [/^\/v1\/atlas\/coverage/, AtlasCoverageResponse],
  [/^\/v1\/links\/path/, LinksPathResponse],
  [/^\/v1\/links\/[^/?]+/, CountryLinksResponse],
  [/^\/v1\/ask/, AskResponse],
];

describe.skipIf(!FILE)('V3 schemas accept the real backend responses', () => {
  const lines = FILE ? readFileSync(FILE, 'utf-8').split('\n').filter(Boolean) : ([] as string[]);
  const records = lines.map((l) => JSON.parse(l) as { method: string; url: string; status: number; body: unknown });

  it('every captured response parses', () => {
    expect(records.length).toBeGreaterThan(0);
    const failures: string[] = [];
    const seen = new Set<string>();
    for (const r of records) {
      const path = r.url.replace(/^https?:\/\/[^/]+/, '');
      const route = ROUTES.find(([re]) => re.test(path));
      if (!route) continue;
      seen.add(route[0].source);
      const res = route[1].safeParse(r.body);
      if (!res.success) failures.push(`${r.method} ${path}\n  ${issuesOf(res.error).join('\n  ')}`);
    }
    expect(failures).toEqual([]);
    expect(seen.size).toBeGreaterThan(5);
  });
});
