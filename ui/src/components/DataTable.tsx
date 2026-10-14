import { useRef, useState, type ReactNode } from 'react';
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from '@tanstack/react-table';
import { useVirtualizer } from '@tanstack/react-virtual';

export interface DataTableProps<T> {
  caption: ReactNode;
  columns: ColumnDef<T, any>[]; // eslint-disable-line @typescript-eslint/no-explicit-any
  rows: T[];
  /** Rows above which the body is virtualised. */
  virtualizeAbove?: number;
  maxHeight?: number;
  rowKey?: (row: T, index: number) => string;
  onRowClick?: (row: T) => void;
  selectedKey?: string | null;
  empty?: ReactNode;
}

const ESTIMATED_ROW = 34;

/**
 * Accessible table (caption + th scope) built on TanStack Table. Large row sets
 * are virtualised with spacer rows so the semantic <table> structure is kept.
 */
export function DataTable<T>({
  caption,
  columns,
  rows,
  virtualizeAbove = 150,
  maxHeight = 480,
  rowKey,
  onRowClick,
  selectedKey,
  empty,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });
  const parentRef = useRef<HTMLDivElement>(null);
  const modelRows = table.getRowModel().rows;
  const virtual = modelRows.length > virtualizeAbove;
  const virtualizer = useVirtualizer({
    count: modelRows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ESTIMATED_ROW,
    overscan: 12,
    enabled: virtual,
  });

  const items = virtual ? virtualizer.getVirtualItems() : null;
  const visible = items ? items.map((i) => modelRows[i.index]!) : modelRows;
  const padTop = items && items.length ? items[0]!.start : 0;
  const padBottom = items && items.length ? virtualizer.getTotalSize() - items[items.length - 1]!.end : 0;
  const colCount = table.getVisibleLeafColumns().length;

  return (
    <div
      ref={parentRef}
      className="table-wrap"
      style={virtual ? { maxHeight, overflowY: 'auto' } : undefined}
      tabIndex={virtual ? 0 : undefined}
      role={virtual ? 'group' : undefined}
      aria-label={virtual ? 'Scrollable table' : undefined}
    >
      <table className="data-table">
        <caption>
          {caption}
          {virtual && <span className="muted"> ({modelRows.length} rows; scroll to view)</span>}
        </caption>
        <thead>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((h) => {
                const sorted = h.column.getIsSorted();
                const canSort = h.column.getCanSort();
                return (
                  <th
                    key={h.id}
                    scope="col"
                    aria-sort={sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : undefined}
                  >
                    {h.isPlaceholder ? null : canSort ? (
                      <button type="button" className="th-sort" onClick={h.column.getToggleSortingHandler()}>
                        {flexRender(h.column.columnDef.header, h.getContext())}
                        <span aria-hidden="true">{sorted === 'asc' ? ' ▲' : sorted === 'desc' ? ' ▼' : ''}</span>
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
          {modelRows.length === 0 && (
            <tr>
              <td colSpan={colCount} className="muted">
                {empty ?? 'No rows returned.'}
              </td>
            </tr>
          )}
          {padTop > 0 && (
            <tr aria-hidden="true">
              <td colSpan={colCount} style={{ height: padTop, padding: 0, border: 0 }} />
            </tr>
          )}
          {visible.map((row) => {
            const key = rowKey ? rowKey(row.original, row.index) : row.id;
            const selected = selectedKey !== undefined && selectedKey === key;
            return (
              <tr
                key={key}
                className={`${onRowClick ? 'clickable' : ''}${selected ? ' selected' : ''}`}
                aria-selected={onRowClick ? selected : undefined}
                tabIndex={onRowClick ? 0 : undefined}
                onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                onKeyDown={
                  onRowClick
                    ? (e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          onRowClick(row.original);
                        }
                      }
                    : undefined
                }
              >
                {row.getVisibleCells().map((cell, ci) =>
                  ci === 0 ? (
                    <th key={cell.id} scope="row">
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </th>
                  ) : (
                    <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                  ),
                )}
              </tr>
            );
          })}
          {padBottom > 0 && (
            <tr aria-hidden="true">
              <td colSpan={colCount} style={{ height: padBottom, padding: 0, border: 0 }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
