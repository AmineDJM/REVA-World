/**
 * Minimal, safe Markdown for board notes: rendered to React elements (never innerHTML), so no markup or
 * script from a note (or from a shared-board URL) can reach the DOM. Supported: # headings, paragraphs,
 * - / 1. lists, > quotes, ``` code blocks, **bold**, *italic*, `code`, [links](https://… or /v8/…).
 */
import type { ReactNode } from 'react';

const SAFE_HREF = /^(https?:\/\/|\/v8(\/|$|\?|#))/i;

export function safeHref(href: string): string | null {
  const h = href.trim();
  return SAFE_HREF.test(h) ? h : null;
}

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  // order matters: code first (its content is literal), then links, bold, italic
  const re = /(`[^`]+`)|(\[([^\]]+)\]\(([^)\s]+)\))|(\*\*([^*]+)\*\*)|(\*([^*]+)\*|_([^_]+)_)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${keyBase}-${i++}`;
    if (m[1]) out.push(<code key={k}>{m[1].slice(1, -1)}</code>);
    else if (m[2]) {
      const href = safeHref(m[4] ?? '');
      out.push(
        href ? (
          <a key={k} href={href} {...(href.startsWith('/') ? {} : { target: '_blank', rel: 'noreferrer noopener' })}>
            {m[3]}
          </a>
        ) : (
          <span key={k} title="Lien refusé (seuls http(s) et /v8 sont permis)">
            {m[3]}
          </span>
        ),
      );
    } else if (m[5]) out.push(<strong key={k}>{m[6]}</strong>);
    else out.push(<em key={k}>{m[8] ?? m[9]}</em>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ source }: { source: string }) {
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let i = 0;
  let n = 0;
  while (i < lines.length) {
    const line = lines[i]!;
    const key = `b${n++}`;
    if (!line.trim()) {
      i++;
      continue;
    }
    if (line.startsWith('```')) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i]!.startsWith('```')) body.push(lines[i++]!);
      i++;
      blocks.push(
        <pre key={key}>
          <code>{body.join('\n')}</code>
        </pre>,
      );
      continue;
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(line);
    if (h) {
      const lvl = h[1]!.length;
      const content = inline(h[2]!, key);
      // notes sit inside an item card (h3): note headings start at h4
      blocks.push(lvl === 1 ? <h4 key={key}>{content}</h4> : lvl === 2 ? <h5 key={key}>{content}</h5> : <h6 key={key}>{content}</h6>);
      i++;
      continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i]!)) items.push(lines[i++]!.replace(/^\s*([-*+]|\d+[.)])\s+/, ''));
      const lis = items.map((t, j) => <li key={j}>{inline(t, `${key}-${j}`)}</li>);
      blocks.push(ordered ? <ol key={key}>{lis}</ol> : <ul key={key}>{lis}</ul>);
      continue;
    }
    if (line.startsWith('>')) {
      const q: string[] = [];
      while (i < lines.length && lines[i]!.startsWith('>')) q.push(lines[i++]!.replace(/^>\s?/, ''));
      blocks.push(<blockquote key={key}>{inline(q.join(' '), key)}</blockquote>);
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i]!.trim() && !/^(#{1,3}\s|```|>|\s*([-*+]|\d+[.)])\s+)/.test(lines[i]!)) para.push(lines[i++]!);
    blocks.push(<p key={key}>{inline(para.join(' '), key)}</p>);
  }
  return <div className="v8b-md">{blocks}</div>;
}
