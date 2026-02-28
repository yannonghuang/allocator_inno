'use client';

import { useState, useMemo } from 'react';

export type Column<T> = {
  key: keyof T | string;
  label: string;
  render?: (row: T) => React.ReactNode;
  sortable?: boolean;
};

type Props<T> = {
  columns: Column<T>[];
  rows: T[];
  filterKeys?: (keyof T | string)[];
  defaultSortKey?: keyof T | string;
  idKey: keyof T | string;
  /** Optional: set id on each <tr> for anchor links (e.g. rowId: (r) => `supply-${r.supply_id}`). */
  rowId?: (row: T) => string | undefined;
  /** Optional: row click handler (e.g. open pegging slide-in). */
  onRowClick?: (row: T) => void;
};

export function SortFilterTable<T extends Record<string, unknown>>({
  columns,
  rows,
  filterKeys,
  defaultSortKey,
  idKey,
  rowId,
  onRowClick,
}: Props<T>) {
  const [filter, setFilter] = useState('');
  const [sortKey, setSortKey] = useState<keyof T | string | null>(defaultSortKey ?? null);
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc');

  const filtered = useMemo(() => {
    if (!filter.trim() || !filterKeys?.length) return rows;
    const q = filter.trim().toLowerCase();
    return rows.filter((row) =>
      filterKeys.some((k) => String(row[k] ?? '').toLowerCase().includes(q))
    );
  }, [rows, filter, filterKeys]);

  const sorted = useMemo(() => {
    if (!sortKey) return filtered;
    return [...filtered].sort((a, b) => {
      const va = a[sortKey];
      const vb = b[sortKey];
      const aNum = typeof va === 'number' ? va : Number(va);
      const bNum = typeof vb === 'number' ? vb : Number(vb);
      if (!Number.isNaN(aNum) && !Number.isNaN(bNum)) {
        return sortDir === 'asc' ? aNum - bNum : bNum - aNum;
      }
      const aStr = String(va ?? '');
      const bStr = String(vb ?? '');
      const cmp = aStr.localeCompare(bStr);
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [filtered, sortKey, sortDir]);

  const handleSort = (key: keyof T | string) => {
    if (sortKey === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    else {
      setSortKey(key);
      setSortDir('asc');
    }
  };

  return (
    <div>
      {filterKeys && filterKeys.length > 0 && (
        <div style={{ marginBottom: '0.5rem' }}>
          <input
            type="text"
            placeholder="Filter..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            style={{ width: '100%', maxWidth: 320 }}
          />
        </div>
      )}
      <div style={{ overflowX: 'auto' }}>
        <table>
          <thead>
            <tr>
              {columns.map((col) => (
                <th key={String(col.key)}>
                  {col.sortable !== false ? (
                    <button
                      type="button"
                      onClick={() => handleSort(col.key)}
                      style={{
                        background: 'none',
                        border: 'none',
                        color: 'inherit',
                        cursor: 'pointer',
                        padding: 0,
                        fontWeight: sortKey === col.key ? 'bold' : 'normal',
                      }}
                    >
                      {col.label} {sortKey === col.key ? (sortDir === 'asc' ? '↑' : '↓') : ''}
                    </button>
                  ) : (
                    col.label
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, i) => (
              <tr
                key={String(row[idKey] ?? i)}
                id={rowId?.(row)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                style={onRowClick ? { cursor: 'pointer' } : undefined}
                role={onRowClick ? 'button' : undefined}
              >
                {columns.map((col) => (
                  <td key={String(col.key)}>
                    {col.render ? col.render(row) : String(row[col.key] ?? '')}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p style={{ fontSize: '0.875rem', color: '#a1a1aa' }}>
        {sorted.length} row{sorted.length !== 1 ? 's' : ''}
        {filter && ` (filtered from ${rows.length})`}
      </p>
    </div>
  );
}
