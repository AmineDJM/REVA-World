import { useEffect, useId, useRef, useState } from 'react';
import type { SemanticProfile } from '../api/types';
import { humanize, temporalSupportLabel, unitLabel } from '../lib/format';

export interface DefinitionSource {
  concept_id: string;
  version?: string;
  definition?: string | null;
  labels?: Record<string, unknown>;
  semantic_profile?: SemanticProfile | null;
}

export interface DefinitionPopoverProps {
  concept: DefinitionSource | null | undefined;
  /** Button text; defaults to "Definition". */
  label?: string;
}

/** Plain-language rows of a concept's semantic profile. */
export function definitionRows(sp: SemanticProfile | null | undefined): [string, string][] {
  if (!sp) return [];
  const rows: [string, string][] = [];
  if (sp.population_universe) rows.push(['Universe', humanize(sp.population_universe)]);
  if (sp.temporal_support) rows.push(['Temporal support', temporalSupportLabel(sp.temporal_support)]);
  if (sp.spatial_support) rows.push(['Spatial support', humanize(sp.spatial_support)]);
  if (sp.aggregation) rows.push(['Aggregation', humanize(sp.aggregation)]);
  if (sp.unit_ref) rows.push(['Unit', `${unitLabel(sp.unit_ref)} (${sp.unit_ref})`]);
  if (sp.currency_basis || sp.price_basis)
    rows.push(['Basis', [sp.currency_basis, sp.price_basis ? humanize(sp.price_basis) : null].filter(Boolean).join(', ')]);
  if (sp.valuation) rows.push(['Valuation', String(sp.valuation)]);
  if (sp.measurement_method) rows.push(['Method', humanize(sp.measurement_method)]);
  if (sp.missingness && sp.missingness.length) rows.push(['Missingness kinds', sp.missingness.map(humanize).join(', ')]);
  return rows;
}

/**
 * Concept definition from its semantic profile: universe, temporal support
 * ("1 Jan point" vs "midyear estimate"), units, basis and valuation (CIF/FOB).
 */
export function DefinitionPopover({ concept, label = 'Definition' }: DefinitionPopoverProps) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        btn.current?.focus();
      }
    };
    const onClick = (e: MouseEvent) => {
      if (!pop.current?.contains(e.target as Node) && !btn.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onClick);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onClick);
    };
  }, [open]);

  if (!concept) {
    return <span className="muted definition-missing">definition not available</span>;
  }
  const title = (concept.labels?.en as string | undefined) ?? concept.concept_id;
  const rows = definitionRows(concept.semantic_profile);

  return (
    <span className="definition-popover">
      <button
        ref={btn}
        type="button"
        className="link-button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((o) => !o)}
      >
        {label}
      </button>
      {open && (
        <div ref={pop} id={id} className="popover" role="dialog" aria-label={`Definition: ${title}`}>
          <p className="popover-title">
            <strong>{title}</strong>{' '}
            <span className="mono muted">
              {concept.concept_id}
              {concept.version ? ` v${concept.version}` : ''}
            </span>
          </p>
          {concept.definition && <p>{concept.definition}</p>}
          {rows.length > 0 ? (
            <dl className="kv compact">
              {rows.map(([k, v]) => (
                <div className="kv-row" key={k}>
                  <dt>{k}</dt>
                  <dd>{v}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="muted">No semantic profile published for this concept.</p>
          )}
        </div>
      )}
    </span>
  );
}
