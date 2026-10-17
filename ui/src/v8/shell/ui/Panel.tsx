import type { ReactNode } from 'react';

/** A titled section. `actions` sit right of the title; `dense` reduces padding; `flush` removes body padding (tables). */
export function Panel({
  title,
  subtitle,
  actions,
  children,
  footer,
  dense = false,
  flush = false,
  id,
  className,
  as: Tag = 'section',
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  dense?: boolean;
  flush?: boolean;
  id?: string;
  className?: string;
  as?: 'section' | 'div' | 'article' | 'aside';
}) {
  const headId = id ? `${id}-title` : undefined;
  return (
    <Tag className={`v8-panel${dense ? ' v8-panel-dense' : ''}${flush ? ' v8-panel-flush' : ''}${className ? ` ${className}` : ''}`} id={id} aria-labelledby={title ? headId : undefined}>
      {(title || actions) && (
        <header className="v8-panel-head">
          <div className="v8-panel-titles">
            {title && (
              <h2 className="v8-panel-title" id={headId}>
                {title}
              </h2>
            )}
            {subtitle && <p className="v8-panel-sub">{subtitle}</p>}
          </div>
          {actions && <div className="v8-panel-actions">{actions}</div>}
        </header>
      )}
      <div className="v8-panel-body">{children}</div>
      {footer && <footer className="v8-panel-foot">{footer}</footer>}
    </Tag>
  );
}
