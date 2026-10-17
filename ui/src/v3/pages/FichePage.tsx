import { Link, useSearchParams } from "react-router";
import { getAtlasSeries, getAtlasTableCells, conceptLabel, type AtlasCell } from "../api/atlas";
import {
  flagsText,
  getEntityFamilies,
  getEntityPlaces,
  type AtlasHint,
  type EntityPlaces,
  type FamilyGroup,
  type LiteralStatement,
} from "../api/families";
import { entityLabel, getKgEntity, getKgTimeline } from "../api/kg";
import { useApi } from "../../lib/useApi";
import { CitationChip } from "../components/CitationChip";
import { EntityCard } from "../components/EntityCard";
import { EntityPicker, type PickedEntity } from "../components/EntityPicker";
import { LicenceBadge, LicenceList, RequiredNotices } from "../components/Licence";
import { Sparkline } from "../components/Sparkline";
import { V3Empty, V3Error, V3Loading } from "../components/States";
import { citeKgStatement } from "../lib/citations";
import {
  FAMILY_NOT_CAUSE,
  levelFr,
  licencesOf,
  literalText,
  project,
  shareFr,
  splitLiterals,
} from "../lib/families";
import { propertyLabel } from "../lib/graph";
import { kindLabel } from "../lib/kinds";
import { temporalLabel, temporalOf } from "../lib/time";

const fiche = (qid: string) => `/v3/fiche?qid=${encodeURIComponent(qid)}`;

/**
 * Fiche — any entity of any kind (country, compound, species, company, region, disease…): labels, identifiers,
 * values, relations grouped by universal family, atlas series (measured_by), space (place chain, map) and time.
 * Every value carries its citation and its licence. No family is causal.
 */
export default function FichePage() {
  const [params, setParams] = useSearchParams();
  const qid = params.get("qid");
  const ent = useApi((s) => getKgEntity(qid!, s), [qid], !!qid);
  const fam = useApi((s) => getEntityFamilies(qid!, {}, s), [qid], !!qid);
  const places = useApi((s) => getEntityPlaces(qid!, s), [qid], !!qid);
  const tl = useApi(
    (s) => getKgTimeline(qid!, { include_incoming: false, limit: 200 }, s),
    [qid],
    !!qid,
  );
  const label =
    ent.status === "ok"
      ? entityLabel(ent.data.data, qid ?? "")
      : (fam.data?.data.label ?? qid ?? "");

  const onPick = (e: PickedEntity | null) => {
    const p = new URLSearchParams(params);
    if (e) p.set("qid", e.qid);
    else p.delete("qid");
    setParams(p);
  };

  return (
    <section className="v3-page v3-fiche" aria-labelledby="fiche-title">
      <header className="v3-page-head">
        <h1 id="fiche-title">Fiche{qid ? ` — ${label}` : ""}</h1>
        <p className="v3-lede">
          Tout ce que les sources disent d’une entité de n’importe quel type,
          relations regroupées par famille, chaque valeur citée avec sa licence.
        </p>
      </header>
      <div className="v3-toolbar">
        <EntityPicker
          label="Entité"
          value={qid ? { qid, label } : null}
          onChange={onPick}
          testId="fiche-picker"
          placeholder="Rechercher une entité (ex. pétrole, Brésil, Q155)"
        />
      </div>
      {!qid && (
        <V3Empty>
          <p>
            <strong>Choisissez une entité</strong> (élément chimique, matériau,
            espèce, entreprise, région, maladie, pays…) pour afficher sa fiche.
          </p>
        </V3Empty>
      )}
      {qid && (
        <>
          {ent.status === "loading" && <V3Loading what="de l’entité" />}
          {ent.status === "error" && (
            <V3Error error={ent.error} what="L’entité" onRetry={ent.reload} />
          )}
          {ent.status === "ok" && (
            <EntityCard entity={ent.data.data} fallbackLabel={qid} />
          )}
          <p
            className="v3-banner v3-banner-path"
            role="note"
            data-testid="families-not-cause"
          >
            <strong>Une relation n’est pas une cause.</strong>{" "}
            {FAMILY_NOT_CAUSE}
          </p>
          {fam.status === "loading" && <V3Loading what="des relations" />}
          {fam.status === "error" && (
            <V3Error
              error={fam.error}
              what="Les relations par famille"
              onRetry={fam.reload}
            />
          )}
          {fam.status === "ok" && (
            <>
              <Literals
                literals={fam.data.data.literals}
                truncated={fam.data.data.literals_truncated}
              />
              <Families
                groups={fam.data.data.families}
                capped={fam.data.data.scan_capped}
              />
              <Series groups={fam.data.data.families} />
            </>
          )}
          <Space
            places={places.status === "ok" ? places.data.data : null}
            loading={places.status === "loading"}
            error={places.status === "error" ? places.error : null}
          />
          <section aria-labelledby="fiche-time">
            <h2 id="fiche-time">Temps</h2>
            {tl.status === "loading" && <V3Loading what="des dates" />}
            {tl.status === "error" && (
              <V3Error error={tl.error} what="La chronologie" />
            )}
            {tl.status === "ok" &&
              (tl.data.data.events.length === 0 ? (
                <p className="muted">
                  Aucune déclaration datée sortante dans les données chargées.
                </p>
              ) : (
                <ol className="v3-fiche-time" data-testid="fiche-time">
                  {tl.data.data.events.slice(0, 12).map((e) => (
                    <li key={e.row_id}>
                      <strong>{temporalLabel(temporalOf(e))}</strong> —{" "}
                      {propertyLabel(e.property)}
                      {e.object ? (
                        <>
                          {" "}
                          <Link to={fiche(e.object.qid)}>{e.object.label}</Link>
                        </>
                      ) : null}{" "}
                      <CitationChip c={citeKgStatement(e)} compact />
                    </li>
                  ))}
                </ol>
              ))}
            <p>
              <Link to={`/v3/frise?qid=${encodeURIComponent(qid)}`}>
                Voir la frise complète
              </Link>
            </p>
          </section>
          {fam.status === "ok" && (
            <>
              <Unmapped items={fam.data.data.unmapped} qid={qid} />
              <LicenceList
                licences={licencesOf(
                  fam.data.data.families,
                  fam.data.data.literals,
                )}
                title="Licences restrictives des valeurs affichées"
              />
            </>
          )}
        </>
      )}
    </section>
  );
}

function Literals({
  literals,
  truncated,
}: {
  literals: LiteralStatement[];
  truncated: boolean;
}) {
  const parts = splitLiterals(literals);
  return (
    <>
      <section aria-labelledby="fiche-ids">
        <h2 id="fiche-ids">Identifiants</h2>
        {parts.identifier.length === 0 ? (
          <p className="muted">Aucun identifiant chargé pour cette entité.</p>
        ) : (
          <LiteralTable rows={parts.identifier} testId="fiche-identifiers" />
        )}
      </section>
      <section aria-labelledby="fiche-values">
        <h2 id="fiche-values">Valeurs</h2>
        {parts.value.length === 0 ? (
          <p className="muted">
            Aucune valeur littérale chargée (absente, pas zéro).
          </p>
        ) : (
          <LiteralTable rows={parts.value} testId="fiche-values" />
        )}
        {truncated && (
          <p className="v3-notice">Liste limitée par le serveur.</p>
        )}
      </section>
    </>
  );
}

function LiteralTable({
  rows,
  testId,
}: {
  rows: LiteralStatement[];
  testId: string;
}) {
  return (
    <table className="v3-table" data-testid={testId}>
      <thead>
        <tr>
          <th scope="col">Propriété</th>
          <th scope="col">Valeur</th>
          <th scope="col">Période</th>
          <th scope="col">Source</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.row_id}>
            <td>{propertyLabel(r.property)}</td>
            <td>{literalText(r.value)}</td>
            <td>{temporalLabel(temporalOf(r))}</td>
            <td>
              <CitationChip c={citeKgStatement(r)} compact />{" "}
              <LicenceBadge rights={r.rights} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Families({
  groups,
  capped,
}: {
  groups: FamilyGroup[];
  capped: boolean;
}) {
  return (
    <section aria-labelledby="fiche-families">
      <h2 id="fiche-families">Relations par famille</h2>
      {groups.length === 0 && (
        <p className="muted">
          Aucune relation classée dans une famille pour cette entité dans les
          données chargées.
        </p>
      )}
      {capped && (
        <p className="v3-notice">
          Entité très connectée : les comptes sont des minima.
        </p>
      )}
      {groups.map((g) => (
        <article
          key={g.family}
          className="v3-family"
          data-testid={`family-${g.family}`}
        >
          <h3>
            {g.label.fr}{" "}
            <span className="muted">
              ({g.count}
              {g.count_is_lower_bound ? " au moins" : ""})
            </span>
          </h3>
          {g.family === "same_as" && (
            <p className="field-hint" data-testid="identity-note">
              Identité, pas une relation : un élément source (Wikidata) porte
              les deux identifiants. REVA ne fusionne pas les fiches ; chacune
              garde ses propres sources.
            </p>
          )}
          {g.family === "corresponds_to" && (
            <p className="field-hint">
              Lignes de tables de correspondance publiées entre nomenclatures :
              une correspondance peut être partielle (indicateurs de la table
              affichés), ce n’est ni une inclusion ni une identité.
            </p>
          )}
          <ul>
            {g.neighbours.map((n) => {
              const share = shareFr(n.share);
              return (
                <li key={n.statement.row_id}>
                  {n.identity && (
                    <span className="v3-badge v3-badge-identity">identité</span>
                  )}{" "}
                  <span className="v3-reading">{n.reading.label.fr}</span>{" "}
                  <Link to={fiche(n.neighbour.qid)}>{n.neighbour.label}</Link>
                  {Object.keys(n.flags).length > 0 && (
                    <span className="v3-flags" data-testid="concordance-flags">
                      {" "}
                      · {flagsText(n.flags)}
                    </span>
                  )}
                  {n.neighbour.kind ? (
                    <span className="v3-chip">
                      {kindLabel(n.neighbour.kind)}
                    </span>
                  ) : null}
                  {share && (
                    <span
                      className="v3-share"
                      title={`part donnée par la source (qualificatif ${n.share!.qualifier})`}
                    >
                      {" "}
                      · part : {share}
                    </span>
                  )}
                  <span className="muted">
                    {" "}
                    · {propertyLabel(n.statement.property)}
                    {n.subtype
                      ? ` (${n.subtype.replaceAll("_", " ")})`
                      : ""} · {temporalLabel(temporalOf(n.statement))}
                  </span>{" "}
                  <CitationChip c={citeKgStatement(n.statement)} compact />{" "}
                  <LicenceBadge rights={n.rights} />
                </li>
              );
            })}
          </ul>
          {g.truncated && (
            <p className="muted">
              {g.shown} affichée(s) sur {g.count}
              {g.count_is_lower_bound ? " au moins" : ""}.
            </p>
          )}
        </article>
      ))}
    </section>
  );
}

function Series({ groups }: { groups: FamilyGroup[] }) {
  const hints = groups
    .filter((g) => g.family === "measured_by")
    .flatMap((g) =>
      g.neighbours
        .map((n) => n.atlas)
        .filter((a): a is AtlasHint => a !== null),
    );
  if (hints.length === 0) return null;
  return (
    <section aria-labelledby="fiche-series">
      <h2 id="fiche-series">Séries de l’atlas</h2>
      <ul data-testid="fiche-series">
        {hints.map((h) =>
          h.kind === "table" && h.source && h.dataset ? (
            <TableSeriesRow key={hintKey(h)} hint={h} />
          ) : (
            <SeriesRow key={hintKey(h)} hint={h} />
          ),
        )}
      </ul>
    </section>
  );
}

const hintKey = (h: AtlasHint) =>
  [h.kind, h.concept, h.table, h.provider_geo, h.iso3, h.indicator_contains, ...h.indicators].join("|");

function SeriesRow({ hint }: { hint: AtlasHint }) {
  const iso3 = hint.iso3;
  const concept = hint.concept ?? "";
  const s = useApi(
    (sig) => getAtlasSeries(concept, iso3!, sig),
    [concept, iso3],
    !!iso3 && !!concept,
  );
  if (!concept) return null;
  if (!iso3) {
    return (
      <li>
        <Link to={`/v3/atlas?concept=${encodeURIComponent(concept)}`}>
          {concept}
        </Link>{" "}
        <span className="muted">
          (série par pays : voir la carte de l’atlas)
        </span>
      </li>
    );
  }
  if (s.status === "loading") return <li>Chargement de {concept}…</li>;
  if (s.status === "error") {
    return (
      <li>
        <V3Error error={s.error} what={`La série ${concept} (${iso3})`} />
      </li>
    );
  }
  if (s.status !== "ok") return null;
  const best = s.data.data.best.filter((c) => c.year !== null);
  const pts = best.map((c) => ({
    year: c.year!,
    value: c.value,
    value_text: c.value_text,
    series: c.series,
    flags: c.flags,
  }));
  const last = best[best.length - 1];
  return (
    <li>
      <strong>{conceptLabel(s.data.data.concept)}</strong> — {iso3} :{" "}
      {pts.length ? (
        <Sparkline points={pts} latestSeries={last?.series} />
      ) : (
        <span className="v3-missing">
          aucune valeur chargée (absente, pas zéro)
        </span>
      )}
      {last && (
        <span>
          {" "}
          {last.year} : {last.value ?? "manquant"} {last.unit}{" "}
          <LicenceBadge rights={last.rights} />
        </span>
      )}
      <RequiredNotices licences={s.data.data.licences} />
    </li>
  );
}

/** A table entity (atlas:<source>.<dataset>): the subject's cells read with the filters its statement carries. */
function TableSeriesRow({ hint }: { hint: AtlasHint }) {
  const s = useApi(
    (sig) =>
      getAtlasTableCells(
        {
          source: hint.source!,
          dataset: hint.dataset!,
          provider_geo: hint.provider_geo,
          iso3: hint.iso3,
          indicators: hint.indicators,
          indicator_contains: hint.indicator_contains,
        },
        sig,
      ),
    [hintKey(hint)],
  );
  const where = hint.provider_geo
    ? `code ${hint.provider_geo}`
    : hint.iso3
      ? hint.iso3
      : hint.indicator_contains
        ? hint.indicator_contains
        : null;
  const title = (
    <>
      Table <code>{hint.table}</code>
      {where && <> — {where}</>}
    </>
  );
  if (s.status === "loading") return <li>Chargement de la table {hint.table}…</li>;
  if (s.status === "error") {
    return (
      <li>
        <V3Error error={s.error} what={`La table ${hint.table}`} />
      </li>
    );
  }
  if (s.status !== "ok") return null;
  const d = s.data.data;
  if (!d.in_snapshot) {
    return (
      <li data-testid="fiche-table-absent">
        {title} :{" "}
        <span className="v3-missing">
          table pas encore dans l’instantané de l’atlas (absente, pas zéro)
        </span>
      </li>
    );
  }
  if (d.indicators.length === 0) {
    return (
      <li>
        {title} :{" "}
        <span className="v3-missing">
          aucune cellule pour ce sujet (absente, pas zéro)
        </span>
      </li>
    );
  }
  const byInd = new Map<string, AtlasCell[]>();
  for (const c of d.cells) {
    const k = c.indicator;
    if (!byInd.has(k)) byInd.set(k, []);
    byInd.get(k)!.push(c);
  }
  return (
    <li data-testid="fiche-table-series">
      {title}
      {d.concepts.length > 0 && (
        <span className="muted">
          {" "}
          (concepts :{" "}
          {d.concepts.slice(0, 3).map((c, i) => (
            <span key={c}>
              {i ? ", " : ""}
              <Link to={`/v3/atlas?concept=${encodeURIComponent(c)}`}>{c}</Link>
            </span>
          ))}
          )
        </span>
      )}
      <ul>
        {d.indicators.map((ind) => {
          const cells = (byInd.get(ind.indicator) ?? []).filter(
            (c) => c.value !== null || c.value_text !== null,
          );
          const annual = cells
            .filter((c) => c.year != null && c.period_basis === "calendar_year")
            .map((c) => ({
              year: c.year!,
              value: c.value,
              value_text: c.value_text,
              series: c.series,
              flags: c.flags,
            }));
          const last = cells[cells.length - 1];
          return (
            <li key={ind.indicator}>
              <code>{ind.indicator}</code> :{" "}
              {annual.length > 1 && <Sparkline points={annual} latestSeries={last?.series} />}
              {last ? (
                <span>
                  {" "}
                  {last.period_start.slice(0, 10)} : {last.value ?? last.value_text} {last.unit}{" "}
                  <LicenceBadge rights={last.rights} />
                </span>
              ) : (
                <span className="muted">
                  {ind.values} valeur(s) sur {ind.geographies} géographie(s), {ind.period_first.slice(0, 10)} →{" "}
                  {ind.period_last.slice(0, 10)}
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {d.truncated && <p className="muted">Cellules tronquées (limite de la réponse).</p>}
      <RequiredNotices licences={d.licences} />
    </li>
  );
}

function Space({
  places,
  loading,
  error,
}: {
  places: EntityPlaces | null;
  loading: boolean;
  error: unknown;
}) {
  return (
    <section aria-labelledby="fiche-space">
      <h2 id="fiche-space">Espace</h2>
      {loading && <V3Loading what="des lieux" />}
      {error ? <V3Error error={error} what="La localisation" /> : null}
      {places && (
        <>
          {places.chain.length <= 1 ? (
            <p className="muted">
              Aucune déclaration « est situé dans » chargée pour cette entité.
            </p>
          ) : (
            <ol className="v3-place-chain" data-testid="fiche-places">
              {places.chain.map((c) => (
                <li key={c.entity.qid}>
                  <Link to={fiche(c.entity.qid)}>{c.entity.label}</Link>
                  {levelFr(c.level) ? (
                    <span className="muted"> ({levelFr(c.level)})</span>
                  ) : null}
                  {c.basis === "iso3166_2_code_prefix" && (
                    <span className="muted">
                      {" "}
                      — pays déduit du préfixe du code ISO 3166-2
                    </span>
                  )}
                  {c.via && <span className="muted"> · via {c.via}</span>}
                  {c.iso3 && (
                    <>
                      {" "}
                      <Link to={`/v3/pays/${c.iso3}`}>dossier {c.iso3}</Link>
                    </>
                  )}
                </li>
              ))}
            </ol>
          )}
          <PointMap places={places} />
        </>
      )}
    </section>
  );
}

function PointMap({ places }: { places: EntityPlaces }) {
  const pts = [
    ...(places.coordinates
      ? [{ c: places.coordinates, label: places.label, own: true }]
      : []),
    ...places.chain
      .slice(1)
      .flatMap((l) =>
        l.coordinates
          ? [{ c: l.coordinates, label: l.entity.label, own: false }]
          : [],
      ),
  ];
  if (pts.length === 0)
    return (
      <p className="muted">Pas de coordonnées dans les données chargées.</p>
    );
  const W = 360;
  const H = 180;
  const first = pts[0]!;
  return (
    <figure className="v3-pointmap">
      <svg
        width={W}
        height={H}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Carte : ${pts.map((p) => `${p.label} (${p.c.latitude.toFixed(2)}°, ${p.c.longitude.toFixed(2)}°)`).join(" ; ")}`}
        data-testid="fiche-map"
      >
        <rect
          x="0"
          y="0"
          width={W}
          height={H}
          fill="none"
          stroke="currentColor"
          strokeOpacity="0.3"
        />
        {[-60, -30, 0, 30, 60].map((lat) => (
          <line
            key={`lat${lat}`}
            x1="0"
            x2={W}
            y1={project(lat, 0, W, H).y}
            y2={project(lat, 0, W, H).y}
            stroke="currentColor"
            strokeOpacity="0.1"
          />
        ))}
        {[-120, -60, 0, 60, 120].map((lon) => (
          <line
            key={`lon${lon}`}
            y1="0"
            y2={H}
            x1={project(0, lon, W, H).x}
            x2={project(0, lon, W, H).x}
            stroke="currentColor"
            strokeOpacity="0.1"
          />
        ))}
        {pts.map((p) => {
          const { x, y } = project(p.c.latitude, p.c.longitude, W, H);
          return (
            <circle
              key={`${p.label}${x}`}
              cx={x}
              cy={y}
              r={p.own ? 4 : 2.5}
              fill={p.own ? "currentColor" : "none"}
              stroke="currentColor"
            />
          );
        })}
      </svg>
      <figcaption className="muted">
        Coordonnées déclarées (P625) ; projection équirectangulaire.{" "}
        <a
          href={`https://www.openstreetmap.org/?mlat=${first.c.latitude}&mlon=${first.c.longitude}#map=6/${first.c.latitude}/${first.c.longitude}`}
          target="_blank"
          rel="noreferrer noopener"
        >
          Ouvrir sur OpenStreetMap
        </a>
      </figcaption>
    </figure>
  );
}

function Unmapped({
  items,
  qid,
}: {
  items: {
    pid: string;
    direction: "out" | "in";
    count: number;
    label_fr: string | null;
    label_en: string | null;
    causal_assertion: boolean;
  }[];
  qid: string;
}) {
  if (items.length === 0) return null;
  return (
    <section aria-labelledby="fiche-unmapped">
      <h2 id="fiche-unmapped">
        Autres relations (non classées dans une famille)
      </h2>
      <ul data-testid="fiche-unmapped">
        {items.map((u) => (
          <li key={`${u.pid}|${u.direction}`}>
            {u.label_fr || u.label_en || u.pid} <code>{u.pid}</code>{" "}
            {u.direction === "in" ? "(entrante)" : "(sortante)"} : {u.count}
            {u.causal_assertion && (
              <>
                {" "}
                — affirmation causale d’une source :{" "}
                <Link to={`/v3/causes?event=${encodeURIComponent(qid)}`}>
                  voir Causes
                </Link>
              </>
            )}
          </li>
        ))}
      </ul>
      <p className="muted">
        Ces relations restent consultables dans{" "}
        <Link to={`/v3/neurones?qid=${encodeURIComponent(qid)}`}>Neurones</Link>
        .
      </p>
    </section>
  );
}
