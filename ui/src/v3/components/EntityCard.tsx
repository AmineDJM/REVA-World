import { Link } from "react-router";
import type { KgEntity } from "../api/kg";
import { entityLabel } from "../api/kg";
import {
  isWikidataSource,
  wikidataItemUrl,
  wikidataRevisionUrl,
} from "../lib/citations";
import { kindGroup, kindLabel } from "../lib/kinds";
import { CommunityBadge, FixtureBadge } from "./Honesty";
import { KindGlyph } from "./KindLegend";

/** Entity card: labels, description, kind, aliases and the provenance of the snapshot. */
export function EntityCard({
  entity,
  fallbackLabel,
}: {
  entity: KgEntity;
  fallbackLabel?: string;
}) {
  const p = entity.provenance;
  const wd = wikidataItemUrl(entity.qid);
  const desc = entity.descriptions.fr || entity.descriptions.en || null;
  const aliases = [
    ...(entity.aliases.fr ?? []),
    ...(entity.aliases.en ?? []),
  ].slice(0, 8);
  const label = entityLabel(entity, fallbackLabel);
  return (
    <div className="v3-entity-card" data-testid="entity-card">
      <h2 className="v3-entity-title">
        <KindGlyph group={kindGroup(entity.kind)} size={18} /> {label}
      </h2>
      <p className="v3-entity-sub">
        <span className="v3-chip">{kindLabel(entity.kind)}</span>{" "}
        {wd ? (
          <a href={wd} target="_blank" rel="noreferrer noopener">
            {entity.qid}
          </a>
        ) : (
          <code>{entity.qid}</code>
        )}
        {entity.labels.en && entity.labels.en !== label ? (
          <span className="muted"> · en : {entity.labels.en}</span>
        ) : null}
      </p>
      {desc ? (
        <p className="v3-entity-desc">{desc}</p>
      ) : (
        <p className="muted">Pas de description dans la source.</p>
      )}
      {aliases.length > 0 && (
        <p className="v3-entity-aliases muted">Aussi : {aliases.join(" · ")}</p>
      )}
      <dl className="v3-dl">
        <dt>Déclarations</dt>
        <dd>
          {entity.statements_out} sortantes · {entity.statements_in} entrantes
          {entity.degree_capped_at &&
          (entity.statements_out >= entity.degree_capped_at ||
            entity.statements_in >= entity.degree_capped_at)
            ? " (au moins)"
            : ""}
        </dd>
        {entity.sitelinks !== null && (
          <>
            <dt>Articles Wikipédia</dt>
            <dd>{entity.sitelinks}</dd>
          </>
        )}
        <dt>Provenance</dt>
        <dd>
          {p ? (
            <>
              {isWikidataSource(p.source_key) ? "Wikidata" : p.source_key}
              {p.source_revision ? (
                <>
                  {" · "}
                  {isWikidataSource(p.source_key) &&
                  wikidataRevisionUrl(p.source_revision) ? (
                    <a
                      href={wikidataRevisionUrl(p.source_revision)!}
                      target="_blank"
                      rel="noreferrer noopener"
                    >
                      révision {p.source_revision}
                    </a>
                  ) : (
                    `révision ${p.source_revision}`
                  )}
                </>
              ) : null}
              {" · récupéré le "}
              {p.retrieved_at.slice(0, 10)}{" "}
              {p.verification === "community_edited_not_verified" && (
                <CommunityBadge />
              )}
              {p.origin === "test_fixture" && <FixtureBadge />}
            </>
          ) : (
            <span className="muted">
              entité connue seulement comme extrémité de déclarations (aucun
              instantané chargé)
            </span>
          )}
        </dd>
      </dl>
      <p className="v3-entity-actions">
        <Link to={`/v3/fiche?qid=${encodeURIComponent(entity.qid)}`}>
          Fiche
        </Link>
        <Link to={`/v3/chemin?from=${encodeURIComponent(entity.qid)}`}>
          Chemin depuis ici
        </Link>
        <Link to={`/v3/frise?qid=${encodeURIComponent(entity.qid)}`}>
          Frise
        </Link>
        <Link to={`/v3/neurones?qid=${encodeURIComponent(entity.qid)}`}>
          Explorer à partir d’ici
        </Link>
      </p>
    </div>
  );
}
