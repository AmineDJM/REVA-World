/**
 * Example queries shown on the empty page: real pairs of the dev database whose paths were checked when the
 * feature was built (docs/dev/v8_v8liens.md). They are queries, not data: the page shows whatever the API answers.
 */
export interface Example {
  domain: string;
  label: string;
  from: string;
  to: string;
  causal?: boolean;
  hops?: number;
}

export const EXAMPLES: Example[] = [
  { domain: 'médicament → maladie', label: 'Imatinib → leucémie myéloïde chronique', from: 'chembl:CHEMBL941', to: 'efo:0000339' },
  { domain: 'pays → pays', label: 'Arabie saoudite → France', from: 'Q851', to: 'Q142', causal: true },
];
