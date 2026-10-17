import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react';

function readRatio(key: string | undefined, fallback: number): number {
  if (!key) return fallback;
  try {
    const v = Number(localStorage.getItem(`v8.split.${key}`));
    return v > 0.05 && v < 0.95 ? v : fallback;
  } catch {
    return fallback;
  }
}

/**
 * Two resizable panes (drag the separator, or focus it and use ←/→ or ↑/↓). Below 720 px the panes stack.
 * `storageKey` remembers the ratio per viewer (localStorage, best effort).
 */
export function Split({
  first,
  second,
  initial = 0.5,
  min = 0.2,
  direction = 'horizontal',
  storageKey,
  label = 'Redimensionner les panneaux',
}: {
  first: ReactNode;
  second: ReactNode;
  initial?: number;
  min?: number;
  direction?: 'horizontal' | 'vertical';
  storageKey?: string;
  label?: string;
}) {
  const [ratio, setRatio] = useState(() => readRatio(storageKey, initial));
  const box = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const clamp = useCallback((r: number) => Math.min(1 - min, Math.max(min, r)), [min]);

  useEffect(() => {
    if (!storageKey) return;
    try {
      localStorage.setItem(`v8.split.${storageKey}`, ratio.toFixed(3));
    } catch {
      /* storage unavailable: ratio is per page view */
    }
  }, [ratio, storageKey]);

  const onMove = (e: PointerEvent) => {
    if (!dragging.current || !box.current) return;
    const r = box.current.getBoundingClientRect();
    setRatio(clamp(direction === 'horizontal' ? (e.clientX - r.left) / r.width : (e.clientY - r.top) / r.height));
  };
  const onKey = (e: KeyboardEvent) => {
    const step = e.shiftKey ? 0.1 : 0.02;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') setRatio((r) => clamp(r - step));
    else if (e.key === 'ArrowRight' || e.key === 'ArrowDown') setRatio((r) => clamp(r + step));
    else return;
    e.preventDefault();
  };
  return (
    <div ref={box} className={`v8-split v8-split-${direction}`} style={{ ['--v8-split' as string]: `${(ratio * 100).toFixed(1)}%` }} onPointerMove={onMove} onPointerUp={() => (dragging.current = false)}>
      <div className="v8-split-pane v8-split-a">{first}</div>
      <div
        className="v8-split-handle"
        role="separator"
        aria-orientation={direction === 'horizontal' ? 'vertical' : 'horizontal'}
        aria-valuenow={Math.round(ratio * 100)}
        aria-valuemin={Math.round(min * 100)}
        aria-valuemax={Math.round((1 - min) * 100)}
        aria-label={label}
        tabIndex={0}
        onKeyDown={onKey}
        onPointerDown={(e) => {
          dragging.current = true;
          (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
        }}
      />
      <div className="v8-split-pane v8-split-b">{second}</div>
    </div>
  );
}
