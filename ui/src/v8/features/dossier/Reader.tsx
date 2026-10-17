import { Fragment, useMemo } from 'react';
import { Link } from 'react-router';
import type { Block, DossierDoc, Footnote, Part, Sentence } from './api';
import { KIND_LABEL, VALUE_KIND_LABEL } from './api';
import { DossierChart } from './Chart';
import { FnRef, footnoteLines } from './Footnote';

/** One run of a sentence: template text, a sourced value (with its footnote chip) or a stated missing value. */
export function PartView({ p, notes }: { p: Part; notes: Map<number, Footnote> }) {
  if (p.t === 'text') return <>{p.text}</>;
  if (p.t === 'missing')
    return (
      <span className="dos-miss" data-testid="dos-missing" title={p.reason ?? undefined}>
        manquant : {p.reason}
      </span>
    );
  const kind = p.kind ?? 'observed';
  const title = [VALUE_KIND_LABEL[kind], p.exact && p.exact !== p.text ? `valeur exacte ${p.exact}` : null].filter(Boolean).join(' · ');
  const inner = p.href ? (
    <Link to={p.href} className="dos-v-link">
      {p.text}
    </Link>
  ) : (
    p.text
  );
  return (
    <span className="dos-v-wrap" data-testid="dos-value">
      <span className={`dos-v dos-v-${kind}`} title={title}>
        {inner}
      </span>
      {p.fn !== null && <FnRef n={p.fn} f={notes.get(p.fn)} />}
    </span>
  );
}

export function SentenceView({ s, notes }: { s: Sentence; notes: Map<number, Footnote> }) {
  return (
    <span className="dos-sentence" data-template={s.template_id}>
      {s.parts.map((p, i) => (
        <PartView key={i} p={p} notes={notes} />
      ))}{' '}
    </span>
  );
}

function BlockView({ b, notes }: { b: Block; notes: Map<number, Footnote> }) {
  switch (b.type) {
    case 'paragraph':
      return (
        <p className="dos-p">
          {b.sentences.map((s, i) => (
            <SentenceView key={i} s={s} notes={notes} />
          ))}
        </p>
      );
    case 'list':
      return (
        <div className="dos-list">
          {b.title && <h4 className="dos-list-title">{b.title}</h4>}
          <ul>
            {b.items.map((s, i) => (
              <li key={i}>
                <SentenceView s={s} notes={notes} />
              </li>
            ))}
            {b.more > 0 && <li className="dos-more">… et {b.more} autre(s) chez la source, non listé(s) ici.</li>}
          </ul>
        </div>
      );
    case 'table':
      return (
        <div className="dos-table-box">
          <table className="dos-table">
            <caption>{b.caption}</caption>
            <thead>
              <tr>
                {b.columns.map((c, i) => (
                  <th key={i} scope="col" className={b.numeric[i] ? 'dos-num' : undefined}>
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {b.rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, ci) => (
                    <td key={ci} className={b.numeric[ci] ? 'dos-num' : undefined}>
                      {c.parts.map((p, pi) => (
                        <PartView key={pi} p={p} notes={notes} />
                      ))}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {b.note && <p className="dos-note">{b.note}</p>}
        </div>
      );
    case 'chart':
      return <DossierChart b={b} notes={notes} />;
    case 'missing':
      return (
        <div className="dos-missing-block" data-testid="dos-missing-block">
          <strong>Manquant — {b.what}</strong> : {b.reason}
        </div>
      );
  }
}

/** The brief as a readable document: answer first, sections, what is not covered, reading rules, sources. */
export function Reader({ doc }: { doc: DossierDoc }) {
  const notes = useMemo(() => new Map(doc.footnotes.map((f) => [f.n, f])), [doc.footnotes]);
  const restricted = doc.licences.filter((l) => l.restricted);
  return (
    <article className="dos-doc" aria-labelledby="dos-title">
      <header className="dos-head">
        <div className="dos-kicker">
          Dossier · {KIND_LABEL[doc.kind] ?? doc.kind}
          {doc.subject.entity_href && (
            <>
              {' · '}
              <Link to={doc.subject.entity_href}>ouvrir la fiche</Link>
            </>
          )}
        </div>
        <h1 id="dos-title" className="dos-title">
          {doc.title}
        </h1>
        <p className="dos-subtitle">
          {doc.subtitle}
          {doc.subject.sublabel ? ` · ${doc.subject.sublabel}` : ''}
        </p>
        <p className="dos-meta">
          {doc.meta.values} valeurs sourcées · {doc.meta.missing} manques déclarés · {doc.footnotes.length} notes · généré le{' '}
          {doc.meta.generated_at.replace('T', ' à ').replace('+00:00', ' UTC')} · instantané {doc.meta.snapshot_id}
          {doc.meta.cached ? ' (en cache)' : ''}
        </p>
        <p className="dos-legend-line" aria-label="Légende des valeurs">
          <span className="dos-v dos-v-observed">observé</span>
          <span className="dos-v dos-v-estimate">estimation publiée</span>
          <span className="dos-v dos-v-projection">projection</span>
          <span className="dos-v dos-v-forecast">prévision scellée</span>
          <span className="dos-v dos-v-scenario">scénario non validé</span>
          <span className="dos-v dos-v-derived">calcul REVA</span>
          <span className="dos-miss">manquant</span>
        </p>
      </header>

      {doc.summary.length > 0 && (
        <section className="dos-essentiel" aria-labelledby="dos-essentiel">
          <h2 id="dos-essentiel">L’essentiel</h2>
          <ul>
            {doc.summary.map((s, i) => (
              <li key={i}>
                <SentenceView s={s} notes={notes} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {doc.sections.map((sec) => (
        <section key={sec.id} id={`dos-s-${sec.id}`} className="dos-section" aria-labelledby={`dos-h-${sec.id}`}>
          <h2 id={`dos-h-${sec.id}`}>{sec.title}</h2>
          {sec.lede && <p className="dos-lede">{sec.lede}</p>}
          {sec.blocks.map((b) => (
            <Fragment key={b.id}>
              <BlockView b={b} notes={notes} />
            </Fragment>
          ))}
        </section>
      ))}

      <section id="dos-s-non-couvert" className="dos-section dos-notcovered" aria-labelledby="dos-h-nc">
        <h2 id="dos-h-nc">Ce que ce dossier ne couvre pas</h2>
        <ul>
          {doc.not_covered.map((n, i) => (
            <li key={i}>
              <span className="dos-miss">{n.what}</span> — {n.reason}
              {n.section && <span className="dos-muted"> ({n.section})</span>}
            </li>
          ))}
        </ul>
      </section>

      <section id="dos-s-regles" className="dos-section" aria-labelledby="dos-h-rules">
        <h2 id="dos-h-rules">Règles de lecture</h2>
        <ul className="dos-rules">
          {doc.honesty.map((h, i) => (
            <li key={i}>{h}</li>
          ))}
        </ul>
        {restricted.length > 0 && (
          <p className="dos-lic-warn">
            Licences restrictives parmi les sources : {restricted.map((l) => `${l.title} (${l.licence})`).join(' ; ')}.
          </p>
        )}
      </section>

      <section id="dos-s-sources" className="dos-section dos-sources" aria-labelledby="dos-h-src">
        <h2 id="dos-h-src">Sources</h2>
        <ol>
          {doc.footnotes.map((f) => (
            <li key={f.n} id={`fn-${f.n}`} value={f.n}>
              {footnoteLines(f)
                .map((l) => (l.k === 'Source' ? l.v : `${l.k.toLowerCase()} : ${l.v}`))
                .join(' · ')}
            </li>
          ))}
        </ol>
      </section>
    </article>
  );
}
