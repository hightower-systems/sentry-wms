function sanitizeCsvValue(val) {
  if (typeof val !== 'string') return val ?? '';
  const escaped = `"${val.replace(/"/g, '""')}"`;
  if (/^[=+\-@\t\r]/.test(val)) return `"'${val.replace(/"/g, '""')}"`;
  return escaped;
}

// Columns that use `render` to display a React element (e.g. <StatusTag>)
// would otherwise serialize to "[object Object]" in the CSV. Prefer an
// explicit `csvValue(row)` when provided; otherwise fall back to the raw
// field value when `render` returned a React element.
function computeCellValue(col, row) {
  if (col.csvValue) return col.csvValue(row);
  if (col.render) {
    const rendered = col.render(row);
    if (rendered === null || rendered === undefined) return row[col.key];
    if (typeof rendered !== 'object') return rendered;
  }
  return row[col.key];
}

export default function DataTable({
  columns,
  data,
  pagination,
  onPageChange,
  onRowClick,
  emptyMessage = 'No records found',
  sortKey,
  sortDir,
  onSort,
}) {
  function exportCSV() {
    if (!data || data.length === 0) return;
    const headers = columns.map((c) => c.label);
    const rows = data.map((row) =>
      columns.map((c) => sanitizeCsvValue(computeCellValue(c, row)))
    );
    const csv = [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${new Date().toISOString().slice(0, 10)}InvExport.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="data-table-wrapper">
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((col) => {
              const isSortable = col.sortable && onSort;
              const isActive = sortKey === col.key;
              return (
                <th
                  key={col.key || col.label}
                  style={isSortable ? { cursor: 'pointer', userSelect: 'none' } : undefined}
                  onClick={isSortable ? () => onSort(col.key) : undefined}
                >
                  {col.label}
                  {isActive && (
                    <span style={{ marginLeft: 4, fontSize: 10 }}>
                      {sortDir === 'asc' ? '\u25B2' : '\u25BC'}
                    </span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {(!data || data.length === 0) ? (
            <tr>
              <td colSpan={columns.length} className="table-empty">
                {emptyMessage}
              </td>
            </tr>
          ) : (
            data.map((row, i) => (
              <tr
                key={row.id || i}
                className={onRowClick ? 'clickable' : ''}
                onClick={() => onRowClick?.(row)}
              >
                {columns.map((col) => (
                  <td key={col.key || col.label} className={col.mono ? 'mono' : ''}>
                    {col.render ? col.render(row) : row[col.key]}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
      {pagination && (
        <div className="pagination">
          <span>
            Page {pagination.page} of {pagination.pages} ({pagination.total} total)
          </span>
          <div className="pagination-buttons">
            <button className="btn-sm btn" onClick={exportCSV} style={{ marginRight: 8 }}>
              Export CSV
            </button>
            <button
              className="pagination-btn"
              disabled={pagination.page <= 1}
              onClick={() => onPageChange(pagination.page - 1)}
            >
              Prev
            </button>
            <button
              className="pagination-btn"
              disabled={pagination.page >= pagination.pages}
              onClick={() => onPageChange(pagination.page + 1)}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
