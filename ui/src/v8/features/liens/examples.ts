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
  { domain: 'gène → maladie → médicament', label: 'CFTR → ivacaftor', from: 'hgnc:1884', to: 'chembl:CHEMBL2010601', causal: true },
  { domain: 'conflit → pays → matière première', label: 'Conflit du Katanga (UCDP) → cobalt', from: 'ucdp:conflict:265', to: 'usgs_myb:commodity:cobalt' },
  { domain: 'espèce → cible → médicament → maladie', label: 'Plasmodium falciparum → paludisme', from: 'ncbitaxon:5833', to: 'efo:0001068' },
  { domain: 'entreprise → pays → centrale', label: 'TotalEnergies SE → combustible nucléaire', from: 'lei:529900S21EQ1BO4ESM68', to: 'gppd:fuel:nuclear' },
  { domain: 'énergie → pays → économie', label: 'Nucléaire → prix à la consommation (France)', from: 'gppd:fuel:nuclear', to: 'effvar:prices.consumer_level@FRA', causal: true },
  { domain: 'pays → pays → inflation', label: 'Arabie saoudite → prix à la consommation (France)', from: 'Q851', to: 'effvar:prices.consumer_level@FRA', causal: true },
  { domain: 'médicament → maladie', label: 'Imatinib → leucémie myéloïde chronique', from: 'chembl:CHEMBL941', to: 'efo:0000339' },
];
