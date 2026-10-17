import { useRef, useState, type MouseEvent, type KeyboardEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { flexRender, getCoreRowModel, getSortedRowModel, useReactTable, type ColumnDef, type RowData, type SortingState } from '@tanstack/react-table';
import { observeElementRect, useVirtualizer } from '@tanstack/react-virtual';

declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    /** Right-aligned, tabular figures. */
    numeric?: boolean;
    /** Fixed column width in px. */
    width?: number;
  }
}

export type { ColumnDef } from '@tanstack/react-table';

export interface VirtualTableProps<T> {
  data: T[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  columns: ColumnDef<T, any>[];
  /** Accessible caption (visually hidden unless `showCaption`). */
  caption: string;
  showCaption?: boolean;
  /** Scroll viewport height (px or CSS length). Rows are virtualised inside it. */
  height?: number | string;
  rowHeight?: number;
  /** Row → V8 URL: rows become links (Enter / click; Ctrl/⌘-click opens a new tab). */
  getRowHref?: (row: T) => string | null;
  onRowClick?: (row: T) => void;
  getRowId?: (row: T, index: number) => string;
  initialSorting?: SortingState;
  sortable?: boolean;
  empty?: ReactNode;
  /** Highlight a row (e.g. the current entity). */
  isRowActive?: (row: T) => boolean;
}

/**
 * Dense, sortable, virtualised table (@tanstack/react-table + @tanstack/react-virtual).
 * Thousands of rows stay smooth; header sticks; numeric columns (meta.numeric) are right-aligned tabular.
 * Missing cells: render <MissingValue compact/> in the cell — never 0.
 */
export function VirtualTable<T>({
  data,
  columns,
  caption,
  showCaption = false,
  height = 420,
  rowHeight = 30,
  getRowHref,
  onRowClick,
  getRowId,
  initialSorting = [],
  sortable = true,
  empty,
  isRowActive,
}: VirtualTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting);
  const navigate = useNavigate();
  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: sortable ? getSortedRowModel() : undefined,
    enableSorting: sortable,
    getRowId,
  });
  const rows = table.getRowModel().rows;
  const scrollRef = useRef<HTMLDivElement>(null);
  const fallbackRect = { width: 960, height: typeof height === 'number' ? height : 420 };
  const virt = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeight,
    overscan: 12,
    initialRect: fallbackRect,
    // An unmeasured box (jsdom, hidden tab) reports 0×0: keep a sane window instead of rendering no row.
    observeElementRect: (instance, cb) => observeElementRect(instance, (rect) => cb(rect.height > 0 ? rect : fallbackRect)),
  });
  const items = virt.getVirtualItems();
  const padTop = items.length ? items[0]!.start : 0;
  const padBottom = items.length ? virt.getTotalSize() - items[items.length - 1]!.end : 0;
  const nCols = table.getVisibleLeafColumns().length;
  const interactive = !!(getRowHref || onRowClick);

  const activate = (row: T, e: MouseEvent | KeyboardEvent) => {
    const href = getRowHref?.(row) ?? null;
    if (href) {
      if (e.ctrlKey || e.metaKey) window.open(href, '_blank', 'noopener');
      else navigate(href);
    } else onRowClick?.(row);
  };

  return (
    <div className="v8-vt" ref={scrollRef} style={{ height, ['--v8-vt-row' as string]: `${rowHeight}px` }} tabIndex={-1}>
      <table className="v8-vt-table" aria-rowcount={rows.length + 1}>
        <caption className={showCaption ? 'v8-vt-caption' : 'v8-sr-only'}>{caption}</caption>
        <thead>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((h) => {
                const meta = h.column.columnDef.meta;
                const sorted = h.column.getIsSorted();
                const canSort = h.column.getCanSort();
                return (
                  <th
                    key={h.id}
                    scope="col"
                    className={meta?.numeric ? 'v8-num-cell' : undefined}
                    style={meta?.width ? { width: meta.width } : undefined}
                    aria-sort={sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : undefined}
                  >
                    {h.isPlaceholder ? null : canSort ? (
                      <button type="button" className="v8-vt-sort" onClick={h.column.getToggleSortingHandler()}>
                        {flexRender(h.column.columnDef.header, h.getContext())}
                        <span className="v8-vt-arrow" aria-hidden="true">
                          {sorted === 'asc' ? '▲' : sorted === 'desc' ? '▼' : ''}
                        </span>
                      </button>
                    ) : (
                      flexRender(h.column.columnDef.header, h.getContext())
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={nCols} className="v8-vt-empty">
                {empty ?? 'Aucune ligne.'}
              </td>
            </tr>
          )}
          {padTop > 0 && (
            <tr aria-hidden="true">
              <td colSpan={nCols} style={{ height: padTop, padding: 0, border: 0 }} />
            </tr>
          )}
          {items.map((vi) => {
            const row = rows[vi.index]!;
            return (
              <tr
                key={row.id}
                aria-rowindex={vi.index + 2}
                className={`${interactive ? 'v8-vt-link' : ''}${isRowActive?.(row.original) ? ' v8-vt-active' : ''}`}
                style={{ height: rowHeight }}
                tabIndex={interactive ? 0 : undefined}
                onClick={interactive ? (e) => activate(row.original, e) : undefined}
                onKeyDown={interactive ? (e) => e.key === 'Enter' && activate(row.original, e) : undefined}
              >
                {row.getVisibleCells().map((c) => (
                  <td key={c.id} className={c.column.columnDef.meta?.numeric ? 'v8-num-cell v8-num' : undefined}>
                    {flexRender(c.column.columnDef.cell, c.getContext())}
                  </td>
                ))}
              </tr>
            );
          })}
          {padBottom > 0 && (
            <tr aria-hidden="true">
              <td colSpan={nCols} style={{ height: padBottom, padding: 0, border: 0 }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
