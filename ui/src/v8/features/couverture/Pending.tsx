import { useMemo, useState } from 'react';
import { Badge, EmptyState, ErrorState, Panel, Skeleton, frNumber, useUrlParam } from '../../shell/ui';
import type { PendingItem, PendingView } from './api';
import { getDecisions, getPending } from './api';
import { useLoad } from './useLoad';
import { STATE_LABEL, STATUS_LABEL, domainLabel, frDateTime, frMb, matchItem } from './model';

async function copy(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

export function CopyButton({ text, label = 'Copier', title }: { text: string; label?: string; title?: string }) {
  const [state, setState] = useState<'idle' | 'ok' | 'err'>('idle');
  return (
    <button
      type="button"
      className="v8-btn v8-btn-sm cv-copy"
      title={title ?? 'Copier dans le presse-papiers'}
      onClick={async () => {
        setState((await copy(text)) ? 'ok' : 'err');
        window.setTimeout(() => setState('idle'), 1600);
      }}
    >
      <span aria-live="polite">{state === 'ok' ? 'Copié ✓' : state === 'err' ? 'Copie impossible' : label}</span>
    </button>
  );
}

function ObservedBadge({ it }: { it: PendingItem }) {
  const o = it.observed;
  if (!o) return null;
  const last = o.checks.map((c) => c.last_retrieved_at).filter(Boolean).sort().pop();
  const title = o.checks
    .map((c) => `${c.connector}${c.min_version ? ` ≥ ${c.min_version}` : ''} : ${c.met ? 'exécuté' : 'non constaté'}${c.versions_acquired.length ? ` (versions ${c.versions_acquired.join(', ')})` : ''}${c.tables_missing.length ? ` ; tables manquantes ${c.tables_missing.join(', ')}` : ''}`)
    .join('\n');
  const kind = o.state === 'done' ? 'win' : o.state === 'partial' ? 'neutral' : 'missing';
  return (
    <Badge kind={kind} title={`Vérifié dans la base (acquisitions par connecteur et tables enregistrées) :\n${title}`}>
      {STATE_LABEL[o.state]}
      {o.state === 'done' && last ? ` · ${frDateTime(last)}` : ''}
    </Badge>
  );
}

export function PendingCard({ it, compact = false, onDomain }: { it: PendingItem; compact?: boolean; onDomain?: (id: string) => void }) {
  const cmds = it.commands.join('\n');
  return (
    <article className={`cv-card${it.observed?.state === 'done' ? ' is-done' : ''}`} aria-labelledby={`cv-it-${it.id}`}>
      <header className="cv-card-head">
        <h4 id={`cv-it-${it.id}`} className="cv-card-title">
          {it.title}
        </h4>
        <div className="cv-card-badges">
          <ObservedBadge it={it} />
          <span className={`cv-pill cv-status-${it.status}`}>{STATUS_LABEL[it.status] ?? it.status}</span>
          <span className={`cv-pill cv-who-${it.who}`}>{it.who === 'owner' ? 'propriétaire' : 'lead'}</span>
          {it.type === 'load' && <span className="cv-pill v8-num" title="Estimation des rapports (octets bruts + stockés) ; tout chargement exige 5 Go libres sur la machine de dev">{frMb(it.disk_mb)}</span>}
        </div>
      </header>
      <p className="cv-unlocks">
        <span className="cv-k">Débloque :</span> {it.unlocks.text}
      </p>
      {!compact && (
        <>
          <p className="cv-action">
            <span className="cv-k">À faire :</span> {it.action}
          </p>
          {(it.unlocks.domains.length > 0 || it.unlocks.tracks.length > 0) && (
            <ul className="cv-tags" aria-label="Domaines et pistes concernés">
              {it.unlocks.domains.map((d) => (
                <li key={d}>
                  {onDomain ? (
                    <button type="button" className="cv-tag" onClick={() => onDomain(d)}>
                      {domainLabel(d)}
                    </button>
                  ) : (
                    <span className="cv-tag">{domainLabel(d)}</span>
                  )}
                </li>
              ))}
              {it.unlocks.tracks.map((t) => (
                <li key={t}>
                  <span className="cv-tag is-track">{t}</span>
                </li>
              ))}
            </ul>
          )}
          {it.commands.length > 0 && (
            <div className="cv-cmds">
              <div className="cv-cmds-head">
                <span className="v8-subtle">Commandes exactes</span>
                <CopyButton text={cmds} label={it.commands.length > 1 ? 'Copier tout' : 'Copier'} />
              </div>
              <pre className="cv-pre">
                <code>{cmds}</code>
              </pre>
            </div>
          )}
          <p className="cv-src v8-subtle">
            Source : {it.source_docs.map((s) => (s.startsWith('lead:') ? 'liste du lead' : s)).join(' · ')}
          </p>
        </>
      )}
    </article>
  );
}

/** "Décisions du propriétaire" (type decision) or "Chargements en attente" (type load), grouped by kind. */
export function PendingTab({ type, onDomain }: { type: 'decision' | 'load'; onDomain: (id: string) => void }) {
  const st = useLoad(`p:${type}`, () => (type === 'decision' ? getDecisions() : getPending()));
  const [q, setQ] = useUrlParam('q', '');
  const [dom, setDom] = useUrlParam('filtre', '');
  const [hideDone, setHideDone] = useUrlParam('masquer', '');
  const v = st.data;
  const groups = useMemo(() => {
    if (!v) return [];
    return v.groups
      .map((g) => ({
        ...g,
        items: g.items.filter(
          (it) =>
            matchItem(q, [it.title, it.action, it.unlocks.text, ...it.commands, ...it.unlocks.atlas_sources, ...it.unlocks.tracks]) &&
            (!dom || it.unlocks.domains.includes(dom)) &&
            !(hideDone === 'faits' && it.observed?.state === 'done'),
        ),
      }))
      .filter((g) => g.items.length > 0);
  }, [v, q, dom, hideDone]);
  const domains = useMemo(() => [...new Set(v?.groups.flatMap((g) => g.items.flatMap((i) => i.unlocks.domains)) ?? [])].sort(), [v]);
  if (st.status === 'error' && !v) return <ErrorState error={st.error} what={type === 'decision' ? 'les décisions du propriétaire' : 'les chargements en attente'} onRetry={st.reload} />;
  if (!v) return <Skeleton height={480} label="Chargement…" />;
  const shown = groups.reduce((a, g) => a + g.items.length, 0);
  return (
    <div className="cv-pending">
      <PendingSummary v={v} type={type} />
      <div className="cv-filters" role="search">
        <input className="v8-input" type="search" placeholder={type === 'decision' ? 'Filtrer : licence, clé, FMI, SIPRI…' : 'Filtrer : gdelt, ucdp, fao…'} value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filtrer la liste" />
        <select className="v8-input" value={dom} onChange={(e) => setDom(e.target.value)} aria-label="Domaine">
          <option value="">Tous les domaines</option>
          {domains.map((d) => (
            <option key={d} value={d}>
              {domainLabel(d)}
            </option>
          ))}
        </select>
        {type === 'load' && (
          <label className="cv-check">
            <input type="checkbox" checked={hideDone === 'faits'} onChange={(e) => setHideDone(e.target.checked ? 'faits' : '')} /> masquer les exécutions constatées
          </label>
        )}
        <span className="v8-subtle v8-num">{shown} affiché{shown > 1 ? 's' : ''}</span>
      </div>
      {groups.length === 0 ? (
        <EmptyState compact title="Rien ne correspond au filtre">
          Effacez le filtre pour revoir toute la liste.
        </EmptyState>
      ) : (
        groups.map((g) => (
          <Panel key={g.kind} title={`${g.label} (${g.items.length})`} dense>
            <div className="cv-cards">
              {g.items.map((it) => (
                <PendingCard key={it.id} it={it} onDomain={onDomain} />
              ))}
            </div>
          </Panel>
        ))
      )}
    </div>
  );
}

function PendingSummary({ v, type }: { v: PendingView; type: 'decision' | 'load' }) {
  if (type === 'decision') {
    const owner = v.groups.reduce((a, g) => a + g.items.filter((i) => i.who === 'owner').length, 0);
    return (
      <p className="cv-answer">
        <strong className="v8-num">{v.counts.open}</strong> décisions ouvertes, dont <strong className="v8-num">{owner}</strong> que seul le propriétaire peut prendre (licences, comptes et clés, autorisations, contacts,
        choix de méthode). Chacune dit ce qu’elle débloque. <span className="v8-subtle">Transcrit des rapports des agents le {v.updated}.</span>
      </p>
    );
  }
  const items = v.groups.flatMap((g) => g.items);
  const disk = items.filter((i) => i.kind === 'disk' && i.observed?.state !== 'done' && i.status !== 'done');
  return (
    <p className="cv-answer">
      <strong className="v8-num">{v.counts.items}</strong> chargements recensés ; <strong className="v8-num">{v.counts.observed_done}</strong> déjà exécutés selon la base
      {v.checked_at ? ` (vérifié ${frDateTime(v.checked_at)})` : ''}. Ceux qui attendent du disque demandent environ <strong className="v8-num">{frMb(v.disk_mb_open_loads)}</strong>
      {v.disk_mb_unknown > 0 ? ` (+ ${frNumber(v.disk_mb_unknown)} sans estimation)` : ''} au total, et chacun exige <strong>5 Go libres</strong> au moment de l’exécution ({disk.length} en attente de disque).
    </p>
  );
}
