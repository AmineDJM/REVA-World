#!/usr/bin/env node
/**
 * V8 per-route bundle report + budgets (owner V8SHELL). Run after `npm run build`:
 *   node scripts/v8-bundle-report.mjs [--json] [--strict]
 * Reads ../src/reva/api/static/.vite/manifest.json. For each route it sums the gzip size of the JS/CSS a
 * cold load of that route fetches: entry (index.html) + V8 shell (V8Root) + the feature page chunk and its
 * static imports. "Δ route" is what a route switch from the shell adds. --strict exits 1 on a budget breach.
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, '../../src/reva/api/static');
const manifest = JSON.parse(readFileSync(join(outDir, '.vite/manifest.json'), 'utf8'));

// Budgets (gzip KB). Shell cold load = React + router + old-app entry + shell; a feature route adds its own code.
const BUDGET = { shellCold: 150, routeDelta: 120, routeDeltaMap: 420 };

const gz = new Map();
const size = (file) => {
  if (!gz.has(file)) gz.set(file, existsSync(join(outDir, file)) ? gzipSync(readFileSync(join(outDir, file))).length : 0);
  return gz.get(file);
};

/** Static closure of a manifest key: JS files + CSS files. */
function closure(key, seen = new Set()) {
  if (seen.has(key) || !manifest[key]) return seen;
  seen.add(key);
  for (const i of manifest[key].imports ?? []) closure(i, seen);
  return seen;
}
const filesOf = (keys) => {
  const js = new Set();
  const css = new Set();
  for (const k of keys) {
    js.add(manifest[k].file);
    for (const c of manifest[k].css ?? []) css.add(c);
  }
  return { js, css };
};
const sum = (set) => [...set].reduce((a, f) => a + size(f), 0);
const kb = (n) => (n / 1024).toFixed(1);

const entryKeys = closure('index.html');
const shellKeys = closure('src/v8/shell/V8Root.tsx', new Set(entryKeys));
const shell = filesOf(shellKeys);
const entry = filesOf(entryKeys);

const featuresDir = join(here, '../src/v8/features');
const features = existsSync(featuresDir) ? readdirSync(featuresDir) : [];
const rows = [];
rows.push({ route: '/ (anciens écrans)', js: sum(entry.js), css: sum(entry.css), delta: 0 });
rows.push({ route: '/v8 (cadre)', js: sum(shell.js), css: sum(shell.css), delta: sum(shell.js) - sum(entry.js) + sum(shell.css) - sum(entry.css) });
for (const id of features) {
  // Feature page chunks = dynamic imports of the feature's index (lazy element) living in its folder.
  const pages = Object.keys(manifest).filter((k) => k.startsWith(`src/v8/features/${id}/`) && manifest[k].isDynamicEntry);
  if (!pages.length) continue;
  const keys = new Set(shellKeys);
  for (const p of pages) closure(p, keys);
  const all = filesOf(keys);
  const js = sum(all.js);
  const css = sum(all.css);
  const usesMap = [...all.js].some((f) => /maplibre|cytoscape/.test(f));
  rows.push({ route: `/v8 → ${id}`, js, css, delta: js + css - sum(shell.js) - sum(shell.css), map: usesMap, pages: pages.length });
}

const breaches = [];
const shellCold = rows[1].js + rows[1].css;
if (shellCold / 1024 > BUDGET.shellCold) breaches.push(`cadre /v8 ${kb(shellCold)} KB > ${BUDGET.shellCold} KB`);
for (const r of rows.slice(2)) {
  const lim = r.map ? BUDGET.routeDeltaMap : BUDGET.routeDelta;
  if (r.delta / 1024 > lim) breaches.push(`${r.route} Δ ${kb(r.delta)} KB > ${lim} KB`);
}

if (process.argv.includes('--json')) {
  console.log(JSON.stringify({ budget: BUDGET, rows, breaches }, null, 2));
} else {
  console.log('route'.padEnd(28), 'JS gz'.padStart(9), 'CSS gz'.padStart(9), 'Δ route'.padStart(9));
  for (const r of rows) console.log(r.route.padEnd(28), kb(r.js).padStart(9), kb(r.css).padStart(9), (r.delta ? kb(r.delta) : '').padStart(9), r.map ? ' (carte/graphe)' : '');
  console.log(`\nBudgets (KB gz): cadre ≤ ${BUDGET.shellCold}, Δ route ≤ ${BUDGET.routeDelta} (≤ ${BUDGET.routeDeltaMap} avec carte/graphe)`);
  console.log(breaches.length ? `DÉPASSEMENTS:\n- ${breaches.join('\n- ')}` : 'Aucun dépassement.');
}
if (process.argv.includes('--strict') && breaches.length) process.exit(1);
