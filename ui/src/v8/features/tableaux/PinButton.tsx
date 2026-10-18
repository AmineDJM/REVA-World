/**
 * « Épingler » button for any feature (public, like pin.ts). The pin API (zod + board store) is loaded only on
 * click, so importing this button costs a few hundred bytes. `item` is computed at click time; returning null
 * disables nothing but reports that there is nothing to pin yet.
 *
 * `linkNote(label, href, lines)` builds a note pin that points back to a V8 URL — the honest pin for views that are
 * not a single entity/series/forecast/decision (a comparison, a path, a brief, a map state, a scoreboard row): the
 * board stores the reference (the URL), never a value.
 */
import { useCallback, useState } from 'react';
import type { PinItem } from './model';

const clean = (s: string) => s.replace(/[[\]\n\r]+/g, ' ').trim();

export function linkNote(label: string, href: string, lines: string[] = []): PinItem {
  const l = clean(label).slice(0, 380) || 'Vue V8';
  const md = [`[${l}](${href.replace(/[()\s]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase().padStart(2, '0')}`)})`, ...lines.map((x) => x.trim()).filter(Boolean)].join('\n\n');
  return { kind: 'note', label: l, markdown: md.slice(0, 20000) };
}

export interface PinButtonProps {
  item: () => PinItem | PinItem[] | null;
  label?: string;
  title?: string;
  className?: string;
}

export function PinButton({ item, label = 'Épingler', title = 'Épingler sur un tableau (Tableaux)', className = 'v8-btn v8-btn-sm' }: PinButtonProps) {
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const onClick = useCallback(async () => {
    const it = item();
    const items = it === null ? [] : Array.isArray(it) ? it : [it];
    if (!items.length) {
      setMsg('Rien à épingler pour l’instant.');
      return;
    }
    setBusy(true);
    try {
      const { pinItem } = await import('./pin');
      let first: Awaited<ReturnType<typeof pinItem>> | null = null;
      for (const x of items) {
        const r = await pinItem(x);
        first ??= r;
      }
      setMsg(first!.added ? `Épinglé sur « ${first!.boardTitle} »${first!.origin === 'local' ? ' (dans ce navigateur)' : ''}.` : `Déjà sur « ${first!.boardTitle} ».`);
    } catch (e) {
      setMsg(`Épinglage impossible : ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [item]);
  return (
    <span className="v8-pin">
      <button type="button" className={className} onClick={() => void onClick()} title={title} disabled={busy} aria-busy={busy}>
        {label}
      </button>
      <span className="v8-pin-msg" role="status" aria-live="polite">
        {msg}
      </span>
    </span>
  );
}
