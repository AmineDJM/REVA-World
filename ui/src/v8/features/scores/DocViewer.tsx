/**
 * Reader for the reports and protocols the scoreboard cites (/v1/v8/scores/doc, whitelisted paths). Markdown is
 * rendered by a tiny safe renderer (headings, paragraphs, lists, tables, code, bold, inline code) built from React
 * elements — never injected HTML. YAML protocols are shown verbatim.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ErrorState, Skeleton } from '../../shell/ui';
import { getDoc, type DocText } from './api';

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    out.push(t.startsWith('**') ? <strong key={`${key}-${i++}`}>{t.slice(2, -2)}</strong> : <code key={`${key}-${i++}`}>{t.slice(1, -1)}</code>);
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function renderMarkdown(md: string): ReactNode[] {
  const src = md.replace(/\r/g, '').split('\n');
  const lines = { length: src.length, at: (j: number): string => src[j] ?? '' };
  const out: ReactNode[] = [];
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines.at(i);
    if (line.startsWith('```')) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !lines.at(i).startsWith('```')) buf.push(lines.at(i++));
      i++;
      out.push(<pre key={k++} className="sc-doc-pre">{buf.join('\n')}</pre>);
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      const level = Math.min(4, (h[1] ?? '#').length + 1);
      const Tag = `h${level}` as 'h2' | 'h3' | 'h4' | 'h5';
      out.push(<Tag key={k++}>{inline(h[2] ?? '', `h${k}`)}</Tag>);
      i++;
      continue;
    }
    if (line.trim().startsWith('|')) {
      const rows: string[][] = [];
      while (i < lines.length && lines.at(i).trim().startsWith('|')) {
        const cells = lines.at(i).trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
        if (!cells.every((c) => /^:?-{2,}:?$/.test(c))) rows.push(cells);
        i++;
      }
      const [head, ...body] = rows;
      out.push(
        <div key={k++} className="sc-doc-table">
          <table>
            {head && (
              <thead>
                <tr>{head.map((c, j) => <th key={j}>{inline(c, `th${k}-${j}`)}</th>)}</tr>
              </thead>
            )}
            <tbody>
              {body.map((r, ri) => (
                <tr key={ri}>{r.map((c, j) => <td key={j}>{inline(c, `td${k}-${ri}-${j}`)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^\s*([*-]|\d+\.)\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*([*-]|\d+\.)\s+/.test(lines.at(i))) {
        let item = lines.at(i).replace(/^\s*([*-]|\d+\.)\s+/, '');
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines.at(i)) && !/^\s*([*-]|\d+\.)\s+/.test(lines.at(i))) item += ` ${lines.at(i++).trim()}`;
        items.push(item);
      }
      out.push(<ul key={k++}>{items.map((it, j) => <li key={j}>{inline(it, `li${k}-${j}`)}</li>)}</ul>);
      continue;
    }
    if (line.trim() === '') {
      i++;
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines.at(i).trim() !== '' && !/^(#{1,4}\s|```|\s*\||\s*([*-]|\d+\.)\s)/.test(lines.at(i))) para.push(lines.at(i++));
    if (para.length === 0) {
      i++;
      continue;
    }
    const text = para.join(' ');
    out.push(text.startsWith('>') ? <blockquote key={k++}>{inline(text.replace(/(^|\s)>\s?/g, ' ').trim(), `q${k}`)}</blockquote> : <p key={k++}>{inline(text, `p${k}`)}</p>);
  }
  return out;
}

export function DocViewer({ path, onClose }: { path: string; onClose: () => void }) {
  const [state, setState] = useState<{ status: 'loading' } | { status: 'ok'; doc: DocText } | { status: 'error'; error: unknown }>({ status: 'loading' });
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const ac = new AbortController();
    setState({ status: 'loading' });
    getDoc(path, ac.signal)
      .then((r) => setState({ status: 'ok', doc: r.data }))
      .catch((error: unknown) => !ac.signal.aborted && setState({ status: 'error', error }));
    return () => ac.abort();
  }, [path]);
  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="sc-doc-overlay" role="presentation" onClick={onClose}>
      <div className="sc-doc" role="dialog" aria-modal="true" aria-label={`Document ${path}`} tabIndex={-1} ref={ref} onClick={(e) => e.stopPropagation()}>
        <header className="sc-doc-head">
          <span className="v8-mono sc-doc-path">{path}</span>
          {state.status === 'ok' && state.doc.sha256 && (
            <span className="v8-mono v8-subtle" title={`sha256 ${state.doc.sha256}`}>
              sha256 {state.doc.sha256.slice(0, 12)}…
            </span>
          )}
          <button type="button" className="v8-btn v8-btn-sm v8-btn-ghost" onClick={onClose} aria-label="Fermer (Échap)">
            Fermer
          </button>
        </header>
        <div className="sc-doc-body">
          {state.status === 'loading' && <Skeleton lines={10} />}
          {state.status === 'error' && <ErrorState error={state.error} what="le document" />}
          {state.status === 'ok' && (state.doc.format === 'markdown' ? <article className="sc-md">{renderMarkdown(state.doc.text)}</article> : <pre className="sc-doc-pre">{state.doc.text}</pre>)}
        </div>
      </div>
    </div>
  );
}
