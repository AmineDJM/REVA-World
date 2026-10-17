import { useCallback, useEffect, useMemo } from 'react';
import { useSearchParams } from 'react-router';
import { Badge, ErrorState, KeyHint, Panel, Skeleton, Stat, Tabs, frCompact, frNumber } from '../../shell/ui';
import type { Column, Matrix, Row } from './api';
import { clearCoverageCache, getDecisions, getMatrix, getPending } from './api';
import { CountryPanel, DomainPanel } from './Detail';
import { FreshnessTab } from './Freshness';
import { HeatMatrix, MatrixLegend } from './Matrix';
import { LEVELS, METRICS, NOTE_SHORT, frDateTime, isLevel, isMetric, setDomainLabels, type Level, type Metric } from './model';
import { CopyButton, PendingTab } from './Pending';
import { useLoad } from './useLoad';
import './couverture.css';

const TABS = ['matrice', 'decisions', 'chargements', 'fraicheur'] as const;
type Tab = (typeof TABS)[number];

/**
 * /v8/couverture — REVA's honest map of what it knows and doesn't. Every state is in the URL:
 *   ?vue=matrice|decisions|chargements|fraicheur  ?niveau=region|subregion|country  ?metrique=pays|lignes|annee
 *   ?domaine=<id>  ?zone=<region or sub-region>  ?pays=<ISO3>  (+ ?q / ?filtre on the lists)
 */
export default function CouverturePage() {
  const [params, setParams] = useSearchParams();
  const vue: Tab = (TABS as readonly string[]).includes(params.get('vue') ?? '') ? (params.get('vue') as Tab) : 'matrice';
  const niveau: Level = isLevel(params.get('niveau') ?? '') ? (params.get('niveau') as Level) : 'region';
  const metrique: Metric = isMetric(params.get('metrique') ?? '') ? (params.get('metrique') as Metric) : 'pays';
  const domaine = params.get('domaine') ?? '';
  const zone = params.get('zone') ?? '';
  const pays = params.get('pays') ?? '';

  const set = useCallback(
    (patch: Record<string, string>, push = true) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(patch)) {
            if (!v) next.delete(k);
            else next.set(k, v);
          }
          return next;
        },
        { replace: !push },
      );
    },
    [setParams],
  );

  const m = useLoad(`m:${niveau}`, () => getMatrix(niveau));
  const dec = useLoad('dec', () => getDecisions());
  const pend = useLoad('pend', () => getPending());
  const matrix = m.data;
  useEffect(() => {
    if (matrix) setDomainLabels(matrix.rows);
  }, [matrix]);

  useEffect(() => {
    document.title = pays ? `${pays} · Couverture` : domaine ? `${matrix?.rows.find((r) => r.id === domaine)?.label ?? domaine} · Couverture` : 'Couverture';
  }, [pays, domaine, matrix]);

  // Escape closes the open detail (country first, then domain).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      if (pays) set({ pays: '' });
      else if (domaine) set({ domaine: '', zone: '' });
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pays, domaine, set]);

  const openDomain = (id: string) => set({ vue: vue === 'matrice' ? '' : vue, domaine: id, zone: '', pays: '' });
  const openCountry = (iso3: string) => set({ pays: iso3 });
  const onCell = (row: Row, col: Column) => {
    if (niveau === 'country') set({ domaine: row.id, pays: col.id, zone: '' });
    else set({ domaine: row.id, zone: col.id, pays: '' });
  };
  const zoneLabel = matrix?.columns.find((c) => c.id === zone)?.label ?? zone;
  const detailOpen = Boolean(domaine || pays);

  return (
    <div className="v8-page cv-page">
      <header className="v8-page-head">
        <h1 className="v8-page-title">Couverture</h1>
        <p className="v8-page-lede">
          Ce que REVA a chargé, par domaine × lieu × période — et ce qui manque, avec ce qu’il faut pour le combler. <strong>{NOTE_SHORT}</strong>
        </p>
      </header>

      <AnswerStrip matrix={matrix} decisionsOpen={dec.data?.counts.open ?? (dec.status === 'error' ? null : undefined)} loads={pend.data ?? (pend.status === 'error' ? null : undefined)} onTab={(t) => set({ vue: t === 'matrice' ? '' : t })} />

      <Tabs
        label="Vues de la couverture"
        idBase="cv"
        value={vue}
        onChange={(t) => set({ vue: t === 'matrice' ? '' : t })}
        tabs={[
          { id: 'matrice', label: 'Matrice' },
          { id: 'decisions', label: 'Décisions du propriétaire', count: dec.data?.counts.open ?? null },
          { id: 'chargements', label: 'Chargements en attente', count: pend.data ? pend.data.counts.items - pend.data.counts.observed_done : null },
          { id: 'fraicheur', label: 'Fraîcheur' },
        ]}
      />

      <div className={`cv-layout${detailOpen ? ' has-detail' : ''}`}>
        <div className="cv-main" id={`cv-panel-${vue}`} role="tabpanel" aria-labelledby={`cv-tab-${vue}`}>
          {vue === 'matrice' && (
            <Panel
              title="Domaines × lieux"
              subtitle={METRICS.find((x) => x.id === metrique)?.hint}
              actions={
                <div className="cv-controls">
                  <Seg label="Niveau" value={niveau} options={LEVELS} onChange={(v) => set({ niveau: v === 'region' ? '' : v, zone: '' }, false)} />
                  <Seg label="Mesure" value={metrique} options={METRICS} onChange={(v) => set({ metrique: v === 'pays' ? '' : v }, false)} />
                </div>
              }
              flush
            >
              {m.status === 'error' && !matrix && <ErrorState error={m.error} what="la matrice de couverture" onRetry={m.reload} />}
              {!matrix && m.status !== 'error' && <Skeleton height={640} label="Chargement de la matrice…" />}
              {matrix && (
                <>
                  <HeatMatrix data={matrix} metric={metrique} onDomain={openDomain} onCell={onCell} onCountry={openCountry} selectedDomain={domaine} selectedCol={pays || zone} />
                  <div className="cv-matrix-foot">
                    <MatrixLegend metric={metrique} />
                    <p className="v8-subtle">
                      <KeyHint keys="←" /> <KeyHint keys="→" /> <KeyHint keys="↑" /> <KeyHint keys="↓" /> naviguer · <KeyHint keys="Enter" /> ouvrir · <KeyHint keys="Esc" /> fermer
                      {niveau === 'country' && ' · * placement REVA hors M49 (survol)'}
                    </p>
                  </div>
                </>
              )}
            </Panel>
          )}
          {vue === 'decisions' && <PendingTab type="decision" onDomain={openDomain} />}
          {vue === 'chargements' && <PendingTab type="load" onDomain={openDomain} />}
          {vue === 'fraicheur' && <FreshnessTab onDomain={openDomain} />}
          {matrix && <Provenance matrix={matrix} onRefreshed={() => { clearCoverageCache(); m.reload(); }} />}
        </div>
        {detailOpen && (
          <aside className="cv-side" aria-label="Détail">
            {pays && <CountryPanel iso3={pays} onClose={() => set({ pays: '' })} onDomain={openDomain} />}
            {domaine && <DomainPanel id={domaine} zone={zone} zoneLabel={zoneLabel} onClose={() => set({ domaine: '', zone: '' })} onCountry={openCountry} onDomain={openDomain} />}
          </aside>
        )}
      </div>
    </div>
  );
}

function Seg<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: { id: T; label: string; hint?: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="cv-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.id} type="button" role="radio" aria-checked={o.id === value} className={`cv-segbtn${o.id === value ? ' is-on' : ''}`} title={o.hint} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function AnswerStrip({
  matrix,
  decisionsOpen,
  loads,
  onTab,
}: {
  matrix: Matrix | null;
  /** undefined = still loading; null = unavailable (error). */
  decisionsOpen: number | null | undefined;
  loads: { counts: { items: number; observed_done: number }; disk_mb_open_loads: number } | null | undefined;
  onTab: (t: Tab) => void;
}) {
  const stats = useMemo(() => {
    if (!matrix) return null;
    const rows = matrix.rows.filter((r) => r.id !== 'other');
    const covered = rows.filter((r) => r.summary.tables > 0 || r.summary.kg_entities > 0).length;
    const empty = rows.filter((r) => r.summary.tables === 0).map((r) => r.label);
    return { covered, total: rows.length, empty };
  }, [matrix]);
  if (!matrix || !stats) return <Skeleton height={92} label="Chargement des totaux…" />;
  const meta = matrix.meta;
  return (
    <section className="cv-strip" aria-label="Réponse en bref">
      <Stat label="Tables d’atlas" value={meta.atlas_tables} size="md" />
      <Stat label="Lignes stockées" value={meta.atlas_rows} size="md" format={(v) => frCompact(Number(v))} />
      <Stat label="Entités KG" value={meta.kg_computed ? meta.kg_entities : null} size="md" format={(v) => frCompact(Number(v))} missing={{ reason: 'passe KG non calculée (--no-kg)' }} />
      <Stat label="Domaines avec au moins une table" value={stats.total - stats.empty.length} size="md" sub={`sur ${stats.total}${stats.empty.length ? ` · sans table : ${stats.empty.join(', ')}` : ''}`} />
      <button type="button" className="cv-strip-btn" onClick={() => onTab('decisions')}>
        {decisionsOpen === undefined ? <Skeleton height={56} label="Chargement des décisions…" /> : <Stat label="Décisions du propriétaire ouvertes" value={decisionsOpen} size="md" missing={{ reason: 'liste indisponible' }} />}
      </button>
      <button type="button" className="cv-strip-btn" onClick={() => onTab('chargements')}>
        {loads === undefined ? (
          <Skeleton height={56} label="Chargement des chargements en attente…" />
        ) : (
          <Stat
            label="Chargements en attente"
            value={loads ? loads.counts.items - loads.counts.observed_done : null}
            size="md"
            sub={loads ? `≈ ${frNumber(loads.disk_mb_open_loads / 1000, 1)} Go, 5 Go libres requis` : undefined}
            missing={{ reason: 'liste indisponible' }}
          />
        )}
      </button>
    </section>
  );
}

function Provenance({ matrix, onRefreshed }: { matrix: Matrix; onRefreshed: () => void }) {
  const meta = matrix.meta;
  const stale = meta.stale?.tables_registered_since ?? 0;
  return (
    <footer className="cv-prov">
      <span>
        Agrégats calculés le <strong>{frDateTime(meta.computed_at)}</strong> sur la base <code>{meta.origin}</code> ({frNumber(meta.places)} pays et territoires, {frNumber(meta.kg_statements)} déclarations KG).
      </span>
      {stale > 0 ? (
        <Badge kind="missing" title={`Dernier enregistrement : ${frDateTime(meta.stale?.last_registered_at)}`}>
          {stale} table{stale > 1 ? 's' : ''} chargée{stale > 1 ? 's' : ''} depuis — à recalculer
        </Badge>
      ) : (
        <Badge kind="observed">à jour avec la base</Badge>
      )}
      <span className="cv-prov-cmd">
        Recalcul : <code>{meta.refresh_command}</code> <CopyButton text={meta.refresh_command} />
        <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={onRefreshed} title="Relire les agrégats (après un recalcul)">
          Relire
        </button>
      </span>
    </footer>
  );
}
