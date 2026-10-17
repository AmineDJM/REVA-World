/** True on Apple platforms (⌘ instead of Ctrl). */
export function isMac(): boolean {
  if (typeof navigator === 'undefined') return false;
  const p = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform ?? '';
  return /mac|iphone|ipad/i.test(p);
}

const GLYPH: Record<string, string> = { Enter: '↵', ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→', Escape: 'Échap', Shift: '⇧' };

/**
 * Keyboard hint: <KeyHint keys="Mod K" /> → ⌘ K (mac) / Ctrl K. Space-separated; "Mod" = ⌘ or Ctrl.
 * A sequence such as "g m" is shown as two keys.
 */
export function KeyHint({ keys, title }: { keys: string | string[]; title?: string }) {
  const list = Array.isArray(keys) ? keys : keys.split(' ').filter(Boolean);
  const mac = isMac();
  return (
    <span className="v8-keys" title={title}>
      {list.map((k, i) => (
        <kbd key={`${k}-${i}`} className="v8-kbd">
          {k === 'Mod' ? (mac ? '⌘' : 'Ctrl') : (GLYPH[k] ?? k)}
        </kbd>
      ))}
    </span>
  );
}
