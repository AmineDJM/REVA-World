import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ProvenanceButton, ProvenanceProvider } from './ProvenanceDrawer';
import { envelope, mockFetch } from '../test/fetchMock';

const EST = '35a3623c-5312-4520-b24a-5ba02fff2220';
const OBS = 'a4e6e6e1-811a-4e5d-a529-c32e220ac13f';
const ACQ = '73fe95a2-a49a-41cc-8625-aa809c2f5e9f';
const SHA = 'b8c35c012e255e3f72d6bf0a080cbbf290abd831cdba1837c819859d07b3f2a9';
const SELECTOR = { match: { date: '2000', countryiso3code: 'FRA' }, artifact_sha256: SHA, json_pointer_hint: '/1/*[countryiso3code=FRA][date=2000]/value' };

const acquisitionNode = {
  id: `acquisition:${ACQ}`,
  kind: 'acquisition',
  label: 'acquisition 2026-10-14 22:36Z',
  attributes: { origin: 'test_fixture', retrieved_at: '2026-10-14T22:36:21Z', raw_sha256: SHA, item_key: 'series:SP.POP.TOTL:p1', status: 200 },
};
const artifactNode = {
  id: `artifact:${SHA}`,
  kind: 'artifact',
  label: 'application/json (19844 bytes)',
  attributes: { sha256: SHA, media_type: 'application/json', byte_size: 19844, rights: { licence: 'CC-BY-4.0' } },
};
const storageNode = {
  id: 'storage_location:506e4fba',
  kind: 'storage_location',
  label: 'file:///objects/raw/sha256/b8/c3',
  attributes: { storage_uri: `file:///objects/raw/sha256/b8/c3/${SHA}`, verified_byte_size: 19844 },
};
const sourceNode = { id: 'source:ffdd', kind: 'source', label: 'worldbank.wdi', attributes: { title: 'World Bank WDI' } };

const estimateGraph = {
  root: `estimate:${EST}`,
  truncated: false,
  max_nodes: 200,
  nodes: [
    { id: `estimate:${EST}`, kind: 'estimate', label: 'demography.population.wdi_midyear_total (selected_report)', attributes: { modality: 'selected_report' } },
    {
      id: `observation:${OBS}`,
      kind: 'observation',
      label: 'wb:SP.POP.TOTL:FRA:2000 r1',
      attributes: { logical_key: 'wb:SP.POP.TOTL:FRA:2000', revision_no: 1, raw_selector: SELECTOR },
    },
    acquisitionNode,
    artifactNode,
    storageNode,
    sourceNode,
  ],
  edges: [
    { source: `estimate:${EST}`, target: `observation:${OBS}`, relation: 'derived_from', attributes: {} },
    { source: `observation:${OBS}`, target: `acquisition:${ACQ}`, relation: 'from_acquisition', attributes: {} },
    { source: `observation:${OBS}`, target: `artifact:${SHA}`, relation: 'selected_from', attributes: { selector: SELECTOR } },
    { source: `acquisition:${ACQ}`, target: `artifact:${SHA}`, relation: 'raw_bytes', attributes: {} },
    { source: `artifact:${SHA}`, target: 'storage_location:506e4fba', relation: 'stored_at', attributes: {} },
    { source: `acquisition:${ACQ}`, target: 'source:ffdd', relation: 'from_source', attributes: {} },
  ],
};

const acquisitionGraph = {
  root: `acquisition:${ACQ}`,
  truncated: false,
  max_nodes: 200,
  nodes: [acquisitionNode, artifactNode, storageNode, sourceNode],
  edges: [
    { source: `acquisition:${ACQ}`, target: `artifact:${SHA}`, relation: 'raw_bytes', attributes: {} },
    { source: `artifact:${SHA}`, target: 'storage_location:506e4fba', relation: 'stored_at', attributes: {} },
    { source: `acquisition:${ACQ}`, target: 'source:ffdd', relation: 'from_source', attributes: {} },
  ],
};

const meta = {
  context_mode: 'belief',
  origin: 'test_fixture',
  modality: 'selected_estimate',
  knowledge_cutoff: '2026-10-15T00:16:30Z',
  knowledge_cutoff_basis: 'release_cutoff',
};

afterEach(() => vi.unstubAllGlobals());

function renderTrace() {
  return render(
    <ProvenanceProvider>
      <ProvenanceButton kind="estimate" id={EST} label="Trace" />
    </ProvenanceProvider>,
  );
}

describe('ProvenanceDrawer drill-through', () => {
  it('renders the chain estimate → observation (selected field) → acquisition → raw artifact → storage, and follows the acquisition', async () => {
    const { calls } = mockFetch({
      [`GET /v1/provenance/estimate/${EST}`]: { body: envelope(estimateGraph, meta) },
      [`GET /v1/provenance/acquisition/${ACQ}`]: { body: envelope(acquisitionGraph, { ...meta, context_mode: 'evidence', modality: 'reported_observation' }) },
    });
    renderTrace();
    const trace = screen.getByRole('button', { name: 'Trace' });
    await userEvent.click(trace);

    const drawer = await screen.findByRole('dialog', { name: 'Provenance' });
    expect(drawer).toHaveAttribute('aria-modal', 'true');
    await waitFor(() => expect(drawer.querySelectorAll('.prov-node').length).toBe(6));
    expect(calls.some((c) => c.url.startsWith(`/v1/provenance/estimate/${EST}`))).toBe(true);
    // Context of the lineage (fixture origin, cutoff) is shown inside the drawer.
    expect(within(drawer).getByRole('region', { name: 'Result context' })).toHaveTextContent('Knowledge cutoff');
    expect(within(drawer).getByTestId('fixture-label')).toHaveTextContent('TEST FIXTURE');

    // Breadth-first from the root: the value comes first, the raw bytes and storage after the source records.
    const kinds = Array.from(drawer.querySelectorAll('.prov-node')).map((li) => li.className.replace('prov-node prov-', ''));
    expect(kinds[0]).toBe('estimate');
    expect(kinds.indexOf('observation')).toBeLessThan(kinds.indexOf('acquisition'));
    expect(kinds.indexOf('observation')).toBeLessThan(kinds.indexOf('artifact'));
    expect(kinds.indexOf('artifact')).toBeLessThan(kinds.indexOf('storage_location'));

    // The observation shows the selected raw field (selector into the provider response).
    const obs = drawer.querySelector('.prov-observation') as HTMLElement;
    expect(within(obs).getByText('Raw selector')).toBeInTheDocument();
    expect(obs).toHaveTextContent('"countryiso3code":"FRA"');
    // The acquisition and the raw artifact carry the same SHA-256; the artifact shows the edge selector.
    const acq = drawer.querySelector('.prov-acquisition') as HTMLElement;
    expect(within(acq).getByText('SHA-256').nextElementSibling).toHaveTextContent(SHA);
    const art = drawer.querySelector('.prov-artifact') as HTMLElement;
    expect(within(art).getByText('SHA-256').nextElementSibling).toHaveTextContent(SHA);
    expect(within(art).getByText('Selector (selected from)').nextElementSibling).toHaveTextContent('json_pointer_hint');
    expect(drawer.querySelector('.prov-storage_location')).toHaveTextContent(SHA);

    // Drill through to the acquisition: a new lineage rooted at the acquisition reaches the same raw bytes.
    await userEvent.click(within(acq).getByRole('button', { name: 'Trace from this acquisition' }));
    await waitFor(() => expect(calls.some((c) => c.url.startsWith(`/v1/provenance/acquisition/${ACQ}`))).toBe(true));
    await waitFor(() => expect(drawer.querySelectorAll('.prov-node').length).toBe(4));
    expect(drawer.querySelector('.prov-node')).toHaveClass('prov-acquisition');
    expect(within(drawer).getByText(ACQ, { exact: false })).toBeInTheDocument();
    expect(drawer.querySelector('.prov-artifact')).toHaveTextContent(SHA);
    expect(drawer.querySelector('.prov-source')).toHaveTextContent('worldbank.wdi');

    // Back returns to the estimate's lineage; Escape closes and restores focus to the trigger.
    await userEvent.click(within(drawer).getByRole('button', { name: 'Back' }));
    await waitFor(() => expect(drawer.querySelectorAll('.prov-node').length).toBe(6));
    expect(within(drawer).queryByRole('button', { name: 'Back' })).toBeNull();
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: 'Provenance' })).toBeNull();
    expect(trace).toHaveFocus();
  });

  it('shows a clear error state when the lineage cannot be loaded', async () => {
    mockFetch({
      [`GET /v1/provenance/estimate/${EST}`]: { status: 404, body: { title: 'not found', status: 404, detail: 'no such estimate', code: 'not_found' } },
    });
    renderTrace();
    await userEvent.click(screen.getByRole('button', { name: 'Trace' }));
    const drawer = await screen.findByRole('dialog', { name: 'Provenance' });
    await waitFor(() => expect(drawer).toHaveTextContent(/not found/i));
    expect(drawer.querySelectorAll('.prov-node').length).toBe(0);
  });

  it('says so instead of rendering a button when a record has no provenance id', () => {
    render(
      <ProvenanceProvider>
        <ProvenanceButton kind="estimate" id={null} label="Trace" />
      </ProvenanceProvider>,
    );
    expect(screen.queryByRole('button', { name: 'Trace' })).toBeNull();
    expect(screen.getByText('no provenance link')).toBeInTheDocument();
  });
});
