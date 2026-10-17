/**
 * The Fiche sections (answer first, then evidence): headline stats, facts, relations by family (paged), series,
 * map & time, causal claims and effect estimates (attributed), forecasts (judged status), what is missing, sources.
 * Every value carries its SourceChip; Missing is hatched with its reason.
 */
import { useState } from 'react';
import { Link } from 'react-router';
import { v8Links } from '../../contract';
import { Badge, EmptyState, MiniLine, MissingValue, SourceChip, Stat, frValue } from '../../shell/ui';
import { getRelations, type Bundle, type Claim, type Effect, type Fact, type MissingItem, type Question, type RelGroup, type RelItem, type Series, type Stat as StatT, type Top } from './api';
import { MiniMap } from './MiniMap';
import { byTheme, chip, gradeTone, linePoints, projectionSplit, SECTION_FR, STATUS_FR } from './model';

// ------------------------------------------------------------------------------------------------ links
export function EntityLink({ kind, id, label, loaded = true }: { kind: string; id: string; label: string; loaded?: boolean }) {
  return (
    <Link to={v8Links.entity(kind, id)} className={`fiche-ent${loaded ? '' : ' fiche-ent-unloaded'}`} title={loaded ? id : `${id} — enregistrement nommé par la source mais pas encore chargé`}>
      {label}
    </Link>
  );
}

function countryHref(iso3: string) {
  return v8Links.entity('country', iso3);
}

// ------------------------------------------------------------------------------------------------ headline
export function Headline({ stats }: { stats: StatT[] }) {
  if (!stats.length) return null;
  return (
    <div className="fiche-stats" data-testid="fiche-headline">
      {stats.map((s) => (
        <HeadlineStat key={s.key} s={s} />
      ))}
    </div>
  );
}

function HeadlineStat({ s }: { s: StatT }) {
  const src = chip(s.source);
  const kind = s.kind === 'projection' ? 'projection' : s.kind === 'estimate' ? 'estimate' : s.kind === 'judged' ? 'judged' : 'observed';
  const line = s.points.length >= 2 ? <MiniLine points={linePoints(s.points)} width={132} height={26} split={projectionSplit(s.points)} label={`Évolution : ${s.label_fr}`} /> : null;
  const sub = (
    <>
      {s.sub && <span>{s.sub}</span>}
      {line}
    </>
  );
  if (s.missing) {
    return (
      <div className="fiche-stat" data-key={s.key}>
        <Stat label={s.label_fr} value={null} missing={{ reason: s.missing.reason_fr }} size="md" />
      </div>
    );
  }
  if (s.value === null) {
    // text, link or relation stat
    return (
      <div className="fiche-stat fiche-stat-text" data-key={s.key}>
        <div className="v8-stat v8-stat-md" data-kind={s.kind}>
          <div className="v8-stat-label">{s.label_fr}</div>
          <div className="v8-stat-value fiche-stat-textval">
            {s.ref ? <EntityLink kind={s.ref.kind} id={s.ref.id} label={s.ref.label} loaded={s.ref.loaded} /> : s.link?.url ? <a href={s.link.url} target="_blank" rel="noreferrer noopener">{s.value_text}</a> : <span>{s.value_text}</span>}
          </div>
          <div className="v8-stat-meta">
            {s.period && <span className="v8-stat-period">{s.period}</span>}
            {s.kind === 'judged' && <Badge kind="judged" size="xs" />}
            {src && <SourceChip {...src} />}
          </div>
          {s.sub && <div className="v8-stat-sub">{s.sub}</div>}
        </div>
      </div>
    );
  }
  const unit = s.kind === 'count' ? (s.unit ? 'au moins' : null) : s.unit;
  return (
    <div className="fiche-stat" data-key={s.key}>
      <Stat
        label={
          s.link?.section ? (
            <a href={`#fiche-${s.link.section}`} className="fiche-stat-anchor">
              {s.label_fr}
            </a>
          ) : (
            s.label_fr
          )
        }
        value={s.value}
        unit={unit}
        period={s.period}
        kind={kind}
        source={src}
        sub={sub}
        maxFractionDigits={unitDigits(s.unit, s.value)}
        format={s.kind === 'count' ? (v) => Number(v).toLocaleString('fr-FR') : undefined}
      />
      {s.ref && s.kind !== 'count' && (
        <div className="fiche-stat-ref">
          <EntityLink kind={s.ref.kind} id={s.ref.id} label={s.ref.label} />
        </div>
      )}
      {s.ref && s.kind === 'count' && <div className="fiche-stat-ref v8-subtle">via {s.ref.label}</div>}
    </div>
  );
}

/** Display digits: integers for big magnitudes, 1–2 decimals for rates. */
function unitDigits(unit: string | null, v: string | null): number {
  const n = v === null ? NaN : Math.abs(Number(v));
  if (Number.isFinite(n) && n >= 1000) return 0;
  if (unit && /%|percent/i.test(unit)) return 1;
  if (Number.isFinite(n) && n >= 10) return 1;
  return 2;
}

// ------------------------------------------------------------------------------------------------ facts
export function Facts({ facts }: { facts: Fact[] }) {
  const rows = facts.filter((f) => !f.long_text);
  const long = facts.filter((f) => f.long_text);
  if (!facts.length) return <EmptyState compact title="Aucune valeur littérale chargée">Les sources chargées ne publient ni quantité, ni date, ni texte pour cet enregistrement (absent, pas zéro).</EmptyState>;
  return (
    <div className="fiche-facts">
      {long.map((f) => (
        <blockquote key={f.property} className="fiche-longtext">
          <span className="fiche-k">{f.label_fr}</span> {f.value_text} <SourceChip {...chip(f.source)!} />
        </blockquote>
      ))}
      <div className="fiche-table-wrap">
        <table className="fiche-table" data-testid="fiche-facts">
          <thead>
            <tr>
              <th scope="col">Propriété</th>
              <th scope="col">Valeur</th>
              <th scope="col">Période</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((f) => (
              <tr key={f.property}>
                <th scope="row" className="fiche-k">
                  {f.label_fr}
                </th>
                <td className="fiche-v">
                  {f.value !== null ? <span className="v8-num">{frValue(f.value, f.unit)}</span> : f.url ? <a href={f.url} target="_blank" rel="noreferrer noopener">{f.value_text}</a> : <span>{f.value_text}</span>}
                  {f.unit && <span className="fiche-unit"> {f.unit}</span>}
                  {f.value_lo && f.value_hi && (
                    <span className="fiche-bounds v8-subtle">
                      {' '}
                      [{frValue(f.value_lo, f.unit)} – {frValue(f.value_hi, f.unit)}]
                    </span>
                  )}
                  {f.points.length >= 2 && (
                    <span className="fiche-fact-line">
                      <MiniLine points={linePoints(f.points)} width={96} height={20} label={`${f.label_fr} : ${f.points.length} valeurs datées`} />
                      <span className="v8-subtle">{f.points.length} val.</span>
                    </span>
                  )}
                  {!f.points.length && f.count > 1 && <span className="v8-subtle" title={f.others.map((o) => `${o.value_text}${o.period ? ` (${o.period})` : ''}`).join(' · ')}> +{f.count - 1}</span>}
                </td>
                <td className="fiche-p v8-num">{f.period ?? <span className="v8-subtle">non daté</span>}</td>
                <td>
                  <SourceChip {...chip(f.source)!} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ relations
export function Relations({ b, kind, id }: { b: Bundle; kind: string; id: string }) {
  const groups = b.relations;
  if (!groups.length && !b.related_forms.length)
    return <EmptyState compact title="Aucune relation classée">Aucune déclaration de source ne relie cet enregistrement à un autre dans une famille de relations (les relations ne sont jamais causales).</EmptyState>;
  return (
    <div className="fiche-rel">
      <p className="fiche-note">Une relation est une déclaration de source, pas une cause.</p>
      <div className="fiche-rel-grid">
        {groups.map((g) => (
          <RelationGroup key={g.family} g={g} kind={kind} id={id} />
        ))}
      </div>
      {b.related_forms.map((f) => (
        <div key={f.ref.id} className="fiche-forms">
          <h3 className="fiche-h3">
            Via <EntityLink kind={f.ref.kind} id={f.ref.id} label={f.ref.label} /> <span className="v8-subtle">({f.relation_fr ?? 'forme liée'}, déclarée par la source)</span>
          </h3>
          <div className="fiche-rel-grid">
            {f.relations.map((g) => (
              <RelationGroup key={g.family} g={g} kind={f.ref.kind} id={f.ref.id} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function RelationGroup({ g, kind, id }: { g: RelGroup; kind: string; id: string }) {
  const [more, setMore] = useState<{ items: RelItem[]; next: string | null; loading: boolean; error: string | null } | null>(null);
  const load = async () => {
    setMore((m) => ({ items: m?.items ?? [], next: m?.next ?? null, loading: true, error: null }));
    try {
      const r = await getRelations(kind, id, g.family, more?.next ?? null);
      setMore((m) => ({ items: [...(m?.items ?? []), ...r.data.items], next: r.next_cursor ?? null, loading: false, error: null }));
    } catch (e) {
      setMore((m) => ({ items: m?.items ?? [], next: m?.next ?? null, loading: false, error: e instanceof Error ? e.message : String(e) }));
    }
  };
  const items = more ? dedupe([...g.items, ...more.items]) : g.items;
  return (
    <section className="fiche-relgroup" data-testid={`fiche-family-${g.family}`} aria-label={g.label_fr}>
      <header className="fiche-relhead">
        <span className="fiche-relname">{g.label_fr}</span>
        <span className="fiche-relcount v8-num" title={g.count_is_lower_bound ? 'Entité très connectée : compte minimal' : undefined}>
          {g.count_is_lower_bound ? '≥ ' : ''}
          {g.count.toLocaleString('fr-FR')}
        </span>
      </header>
      <ul className="fiche-rellist">
        {items.map((it, i) => (
          <li key={`${it.ref.id}|${it.property}|${i}`}>
            <span className="fiche-reading">{it.reading_fr}</span> <EntityLink kind={it.ref.kind} id={it.ref.id} label={it.ref.label} loaded={it.ref.loaded} />
            {it.share && <span className="v8-subtle"> · part {it.share}{it.share_unit ? ` ${it.share_unit}` : ''}</span>}
            {it.period && <span className="v8-subtle v8-num"> · {it.period}</span>}
            <SourceChip {...chip(it.source)!} />
          </li>
        ))}
      </ul>
      {(g.more || more?.next) && (
        <div className="fiche-relmore">
          {more?.error && <span className="fiche-err">{more.error}</span>}
          <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={load} disabled={more?.loading}>
            {more?.loading ? 'Chargement…' : `Afficher plus (${items.length} / ${g.count_is_lower_bound ? '≥ ' : ''}${g.count.toLocaleString('fr-FR')})`}
          </button>
        </div>
      )}
    </section>
  );
}

function dedupe(items: RelItem[]): RelItem[] {
  const seen = new Set<string>();
  return items.filter((it) => {
    const k = `${it.ref.id}|${it.property}|${it.source.statement_key ?? ''}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

// ------------------------------------------------------------------------------------------------ series
export function SeriesSection({ series, tops }: { series: Series[]; tops: Top[] }) {
  if (!series.length && !tops.length)
    return <EmptyState compact title="Aucune série liée">Aucune table de l’atlas ni valeur datée n’est reliée à cet enregistrement dans les données chargées (voir « Manques »).</EmptyState>;
  return (
    <div className="fiche-series">
      {tops.map((t) => (
        <TopTable key={t.key} t={t} />
      ))}
      {byTheme(series).map(([theme, items]) => (
        <div key={theme} className="fiche-theme">
          <h3 className="fiche-h3">{theme}</h3>
          <div className="fiche-series-grid">
            {items.map((s) => (
              <SeriesCard key={s.key} s={s} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function SeriesCard({ s }: { s: Series }) {
  const href = s.link?.table ? v8Links.series(s.link.table, s.link.params ?? {}) : null;
  return (
    <article className={`fiche-scard${s.missing ? ' fiche-scard-missing' : ''}`} data-testid="fiche-series-card">
      <header className="fiche-scard-head">
        <span className="fiche-scard-title" title={s.key}>
          {s.label_fr}
        </span>
        {href && (
          <Link to={href} className="fiche-open" title="Ouvrir dans Monde">
            Monde ↗
          </Link>
        )}
      </header>
      {s.missing ? (
        <MissingValue reason={s.missing.reason_fr} />
      ) : (
        <>
          <div className="fiche-scard-val">
            {s.value !== null ? <span className={`v8-num fiche-big${s.kind === 'projection' ? ' fiche-proj' : ''}`}>{frValue(s.value, s.unit)}</span> : <span>{s.value_text}</span>}
            {s.unit && <span className="fiche-unit">{s.unit}</span>}
          </div>
          <div className="fiche-scard-line">
            <MiniLine points={linePoints(s.points)} width={220} height={34} split={projectionSplit(s.points)} label={`${s.label_fr}, ${s.first ?? ''}–${s.last ?? ''}`} />
          </div>
          <footer className="fiche-scard-foot">
            <span className="v8-num">{s.period}</span>
            {s.kind === 'projection' && <Badge kind="projection" size="xs" />}
            {s.kind === 'derived' && <Badge kind="estimate" size="xs" title="Valeur dérivée par REVA à partir de séries sourcées">dérivé</Badge>}
            {s.out_of_snapshot && <Badge kind="info" size="xs" title="Lu dans la table publiée la plus récente, pas encore épinglée dans l’instantané atlas">hors instantané</Badge>}
            {s.source_breaks > 0 && <Badge kind="info" size="xs" title="La source qui répond change dans la série (rupture signalée)">{`${s.source_breaks} rupture${s.source_breaks > 1 ? 's' : ''}`}</Badge>}
            <span className="v8-subtle">{s.n_points} pts{s.first && s.last ? ` · ${s.first}–${s.last}` : ''}</span>
            {s.source && <SourceChip {...chip(s.source)!} />}
          </footer>
        </>
      )}
    </article>
  );
}

function TopTable({ t }: { t: Top }) {
  return (
    <div className="fiche-top" data-testid="fiche-top">
      <h3 className="fiche-h3">
        {t.label_fr}
        {t.year !== null && <span className="v8-subtle"> · {t.year} · {t.n_countries} pays renseignés</span>}
      </h3>
      {t.items.length === 0 ? (
        <MissingValue reason="aucune année récente avec au moins 20 pays renseignés" />
      ) : (
        <ol className="fiche-toplist">
          {t.items.map((i) => (
            <li key={i.iso3}>
              <Link to={countryHref(i.iso3)}>{i.iso3}</Link> <span className="v8-num">{i.value ? frValue(i.value, t.unit) : '—'}</span> <span className="fiche-unit">{t.unit}</span> <SourceChip {...chip(i.source)!} />
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ map & time
export function SpaceTime({ b }: { b: Bundle }) {
  const sp = b.space;
  const label = b.identity?.label ?? b.canonical.id;
  const hasMap = !!sp.geometry || !!sp.point;
  return (
    <div className="fiche-spacetime">
      <div className="fiche-place">
        {hasMap ? (
          <figure className="fiche-mapfig">
            <MiniMap geometry={(sp.geometry?.geojson as { type: string; coordinates: unknown }) ?? null} point={sp.point} label={label} />
            <figcaption className="v8-subtle">
              {sp.geometry ? sp.geometry.note_fr : ''}
              {sp.point ? ` Point : coordonnées déclarées (${sp.point.source_key ?? 'source'}).` : ''}
            </figcaption>
          </figure>
        ) : (
          <MissingValue reason="pas de coordonnées ni de contour d’affichage dans les données chargées" block />
        )}
        {sp.chain.length > 0 ? (
          <ol className="fiche-chain" data-testid="fiche-places" aria-label="Situé dans">
            {sp.chain.map((c) => (
              <li key={c.ref.id}>
                {c.iso3 ? <Link to={countryHref(c.iso3)}>{c.ref.label}</Link> : <EntityLink kind={c.ref.kind} id={c.ref.id} label={c.ref.label} loaded={c.ref.loaded} />}
                {c.level_fr && <span className="v8-subtle"> ({c.level_fr})</span>}
              </li>
            ))}
          </ol>
        ) : (
          <p className="v8-subtle">Aucune déclaration « est situé dans » pour cet enregistrement.</p>
        )}
      </div>
      <div className="fiche-time">
        <h3 className="fiche-h3">Dates</h3>
        {b.time.length === 0 ? (
          <p className="v8-subtle">Aucune déclaration datée (hors valeurs déjà tracées dans « Faits »).</p>
        ) : (
          <ol className="fiche-timeline" data-testid="fiche-time">
            {b.time.map((e, i) => (
              <li key={`${e.property}|${e.date}|${i}`}>
                <span className="fiche-date v8-num">{e.date ?? 'non daté'}</span>
                <span className="fiche-ev">
                  {e.label_fr}
                  {e.ref ? (
                    <>
                      {' '}
                      <EntityLink kind={e.ref.kind} id={e.ref.id} label={e.ref.label} loaded={e.ref.loaded} />
                    </>
                  ) : e.value_text && e.value_text !== e.date ? (
                    <> : {e.value_text}</>
                  ) : null}
                </span>
                <SourceChip {...chip(e.source)!} />
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ causes & effects
export function Causes({ b }: { b: Bundle }) {
  const c = b.claims;
  const e = b.effects;
  const empty = !c.n_in && !c.n_out && !e.items.length;
  return (
    <div className="fiche-causes">
      <p className="fiche-note">{c.note_fr}</p>
      {empty && <EmptyState compact title="Aucune affirmation causale ni estimation d’effet">Aucune source chargée n’affirme une cause ou un effet impliquant cet enregistrement.</EmptyState>}
      {(c.n_in > 0 || c.n_out > 0) && (
        <div className="fiche-claims-grid">
          <ClaimList title="Ce que des sources disent en être la cause" n={c.n_in} items={c.incoming} side="in" />
          <ClaimList title="Ce que des sources disent qu’il cause" n={c.n_out} items={c.outgoing} side="out" />
        </div>
      )}
      {e.items.length > 0 && (
        <div className="fiche-effects">
          <h3 className="fiche-h3">
            Estimations d’effet attribuées <span className="v8-subtle">({e.items.length}{e.n_country_scope ? ` · ${e.n_country_scope} portant sur ce pays` : ''})</span>
          </h3>
          <p className="fiche-note">{e.note_fr}</p>
          <ul className="fiche-effectlist">
            {e.items.map((x) => (
              <EffectRow key={x.estimate_key} e={x} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ClaimList({ title, n, items, side }: { title: string; n: number; items: Claim[]; side: 'in' | 'out' }) {
  return (
    <section className="fiche-claims" aria-label={title}>
      <h3 className="fiche-h3">
        {title} <span className="v8-subtle v8-num">({n})</span>
      </h3>
      {items.length === 0 ? (
        <p className="v8-subtle">Aucune.</p>
      ) : (
        <ul className="fiche-claimlist">
          {items.map((c) => {
            const other = side === 'in' ? c.cause : c.effect;
            return (
              <li key={c.claim_key} className="fiche-claim">
                <div className="fiche-claim-head">
                  <Badge kind={gradeTone(c.grade)} size="xs" title={c.grade_reasons_fr.join(' · ') || 'note du barème causal REVA'}>
                    {c.grade ? `note ${c.grade}` : 'non noté'}
                  </Badge>
                  <span className="fiche-tier" title={c.tier_fr ?? undefined}>
                    {c.tier}
                  </span>
                  <EntityLink kind={other.kind} id={other.id} label={other.label} loaded={other.loaded} />
                  {c.contested && <Badge kind="loss" size="xs">contesté</Badge>}
                </div>
                <p className="fiche-wording">{c.wording_fr}</p>
                {c.attribution_fr && <p className="v8-subtle fiche-small">{c.attribution_fr}</p>}
                <SourceChip {...chip(c.source)!} />
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function EffectRow({ e }: { e: Effect }) {
  return (
    <li className="fiche-effect">
      <div className="fiche-effect-head">
        <Badge kind="estimate" size="xs" />
        <span>
          {e.cause_fr} → {e.effect_fr}
        </span>
        {e.point !== null ? (
          <span className="v8-num fiche-effect-val">
            {e.point}
            {e.ci_low !== null && e.ci_high !== null && (
              <span className="v8-subtle">
                {' '}
                [{e.ci_low} ; {e.ci_high}]
              </span>
            )}
          </span>
        ) : (
          <MissingValue reason="valeur ponctuelle non publiée par la source" compact />
        )}
        {e.countries.length > 0 && <span className="v8-subtle">{e.countries.slice(0, 6).join(', ')}</span>}
      </div>
      <p className="fiche-wording">{e.wording_fr}</p>
      <SourceChip {...chip(e.source)!} />
    </li>
  );
}

// ------------------------------------------------------------------------------------------------ forecasts
export function Forecasts({ b }: { b: Bundle }) {
  const f = b.forecasts;
  const warming = b.meta.warming.includes('forecasts');
  if (warming) return <EmptyState compact title="Registre des prévisions en cours d’indexation">Premier accès de ce serveur : les lots scellés sont en cours de lecture. Rechargez la page dans une minute.</EmptyState>;
  if (!f.questions.length)
    return <EmptyState compact title="Aucune question de prévision">Le registre prospectif ne contient aucune question sur cet enregistrement.</EmptyState>;
  return (
    <div className="fiche-fc">
      <p className="fiche-note">
        {f.note_fr} <span className="v8-num">{f.n_total}</span> question{f.n_total > 1 ? 's' : ''} · {f.n_pending} en attente · {f.n_resolved} résolue{f.n_resolved > 1 ? 's' : ''}
        {f.questions.length < f.n_total ? ` (les ${f.questions.length} premières affichées)` : ''}.
      </p>
      <div className="fiche-fc-grid">
        {f.questions.map((q) => (
          <QuestionCard key={q.question_id} q={q} />
        ))}
      </div>
    </div>
  );
}

function QuestionCard({ q }: { q: Question }) {
  return (
    <article className="fiche-q" data-testid="fiche-question">
      <header className="fiche-q-head">
        <Link to={v8Links.entity('question', q.question_id)} className="fiche-q-title">
          {q.label_fr ?? q.question_id}
        </Link>
        <span className="v8-num fiche-q-period">{q.reference_period}</span>
        {q.status === 'resolved' ? <Badge kind="judged" size="xs" /> : <Badge kind="info" size="xs">{STATUS_FR[q.status] ?? q.status}</Badge>}
      </header>
      <p className="fiche-q-judged">{q.judged_fr}</p>
      {q.type && q.type !== 'quantity' && (
        <p className="v8-subtle fiche-small">
          Type : {q.type}
          {q.threshold ? ` · seuil ${q.threshold}` : ''}
        </p>
      )}
      <div className="fiche-table-wrap">
        <table className="fiche-table fiche-table-sm">
          <thead>
            <tr>
              <th scope="col">Prévisionniste</th>
              <th scope="col">Valeur</th>
              <th scope="col">Intervalle</th>
              {q.status === 'resolved' && <th scope="col">Score</th>}
            </tr>
          </thead>
          <tbody>
            {q.forecasts.map((r) => (
              <tr key={r.model} className={r.kind === 'official' ? 'fiche-official' : undefined}>
                <th scope="row" title={r.description ?? undefined}>
                  <span className={`fiche-mkind fiche-mkind-${r.kind}`}>{r.kind_fr}</span> {r.label}
                </th>
                <td className="v8-num">
                  {r.point !== null ? frValue(r.point, q.unit) : r.probability !== null ? `p = ${r.probability}` : r.status === 'issued' ? <MissingValue reason="point non publié" compact /> : <span className="v8-subtle">{r.status}</span>}
                </td>
                <td className="v8-num v8-subtle">{r.lo !== null && r.hi !== null ? `${frValue(r.lo, q.unit)} – ${frValue(r.hi, q.unit)}${r.level ? ` (${r.level})` : ''}` : '—'}</td>
                {q.status === 'resolved' && <td className="v8-num">{r.score ? Object.entries(r.score).map(([k, v]) => `${k} ${v ?? '—'}`).join(' · ') : '—'}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <footer className="fiche-q-foot v8-subtle">
        {q.unit && <span>{q.unit}</span>}
        {q.expected_resolution && <span> · résolution attendue {q.expected_resolution}</span>}
        {q.outcome !== null && <span> · réalisé : <strong className="v8-num">{frValue(q.outcome, q.unit)}</strong></span>}
        {q.family && (
          <>
            {' '}
            · <Link to={v8Links.scoreboard(q.family)}>scores de la famille</Link>
          </>
        )}
      </footer>
    </article>
  );
}

// ------------------------------------------------------------------------------------------------ missing & sources
export function Missing({ items }: { items: MissingItem[] }) {
  if (!items.length) return <p className="v8-subtle">Rien de ce que REVA attend pour ce type d’entité ne manque dans les données chargées.</p>;
  return (
    <ul className="fiche-missing" data-testid="fiche-missing">
      {items.map((m, i) => (
        <li key={`${m.section}|${m.what_fr}|${i}`}>
          <span className="fiche-missing-what">
            <span className="fiche-sec-chip">{SECTION_FR[m.section] ?? m.section}</span> {m.what_fr}
          </span>
          <MissingValue reason={m.reason_fr} />
        </li>
      ))}
    </ul>
  );
}

export function Sources({ b }: { b: Bundle }) {
  if (!b.sources.length) return <p className="v8-subtle">Aucune source.</p>;
  return (
    <div className="fiche-table-wrap">
      <table className="fiche-table" data-testid="fiche-sources">
        <thead>
          <tr>
            <th scope="col">Source</th>
            <th scope="col">Valeurs affichées</th>
            <th scope="col">Licence</th>
            <th scope="col">Statut</th>
          </tr>
        </thead>
        <tbody>
          {b.sources.map((s) => (
            <tr key={s.source}>
              <th scope="row">
                {s.label} <span className="v8-subtle v8-mono">{s.source}</span>
              </th>
              <td className="v8-num">{s.n_values}</td>
              <td>
                {s.licences.join(' ; ') || <span className="v8-subtle">licence non enregistrée (inconnue, pas ouverte)</span>}
                {s.non_commercial && <Badge kind="licence" size="xs" />}
                {s.restricted && !s.non_commercial && (
                  <Badge kind="licence" size="xs" title="Conditions restrictives (partage à l’identique, pas de modification ou licence non ouverte)">
                    licence restrictive
                  </Badge>
                )}
              </td>
              <td>{s.verification === 'community_edited_not_verified' ? <Badge kind="community" size="xs" /> : <span className="v8-subtle">{s.verification === 'reva_grouping' ? 'regroupement REVA' : 'non vérifié par REVA'}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
