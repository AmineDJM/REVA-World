import type { CSSProperties, ReactNode } from 'react';

/**
 * Empty state: say WHAT is missing and WHY (never invent demo numbers). `action` is a button or link.
 */
export function EmptyState({ title, children, action, icon = '∅', compact = false }: { title: ReactNode; children?: ReactNode; action?: ReactNode; icon?: ReactNode; compact?: boolean }) {
  return (
    <div className={`v8-empty${compact ? ' v8-empty-compact' : ''}`} role="status">
      <span className="v8-empty-icon" aria-hidden="true">
        {icon}
      </span>
      <div className="v8-empty-body">
        <p className="v8-empty-title">{title}</p>
        {children && <div className="v8-empty-text">{children}</div>}
        {action && <div className="v8-empty-action">{action}</div>}
      </div>
    </div>
  );
}

/**
 * Loading placeholder with a FIXED footprint (no layout shift): give it the height of the content it stands for.
 */
export function Skeleton({ lines = 3, height, width, label = 'Chargement…', style }: { lines?: number; height?: number | string; width?: number | string; label?: string; style?: CSSProperties }) {
  if (height !== undefined) {
    return <div className="v8-skel v8-skel-block" style={{ height, width, ...style }} role="status" aria-label={label} />;
  }
  return (
    <div className="v8-skel-lines" role="status" aria-label={label} style={{ width, ...style }}>
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="v8-skel v8-skel-line" style={{ width: `${i === lines - 1 && lines > 1 ? 62 : 100}%` }} />
      ))}
    </div>
  );
}
