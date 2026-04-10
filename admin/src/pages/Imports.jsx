import { useState, useRef } from 'react';
import { api } from '../api.js';
import PageHeader from '../components/PageHeader.jsx';

const CSV_TEMPLATES = {
  items: `sku,name,description,upc,default_bin,quantity
WIDGET-001,Blue Widget,Standard blue widget,012345678901,A-01-01-01,100
WIDGET-002,Red Widget,Standard red widget,012345678902,A-01-01-02,50
GADGET-001,Mini Gadget,Compact gadget device,012345678903,B-02-01-01,200`,
  'purchase-orders': `po_number,vendor,sku,quantity,expected_date
PO-1001,Acme Supply Co,WIDGET-001,100,2026-05-01
PO-1001,Acme Supply Co,WIDGET-002,50,2026-05-01
PO-1002,Global Parts Inc,GADGET-001,200,2026-05-15`,
  'sales-orders': `so_number,customer,customer_phone,customer_address,sku,quantity
SO-5001,John Smith,555-0101,123 Main St,WIDGET-001,2
SO-5001,John Smith,555-0101,123 Main St,GADGET-001,1
SO-5002,Jane Doe,555-0102,456 Oak Ave,WIDGET-002,3`,
  bins: `bin_code,zone,aisle,bin_type,pick_sequence,description
C-01-01-01,STORAGE,C,Pickable,100,Shelf C Row 1 Level 1
C-01-02-01,STORAGE,C,Pickable,101,Shelf C Row 2 Level 1
D-01-01-01,PICKING,D,Pickable,200,Pick zone D`,
};

function downloadTemplate(type) {
  const csv = CSV_TEMPLATES[type];
  if (!csv) return;
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `import-${type}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

const IMPORT_TYPES = [
  { value: 'items', label: 'Items', desc: 'Import products with SKU, UPC, weight, and initial quantity' },
  { value: 'bins', label: 'Bins', desc: 'Import bin locations with zone, aisle, and type' },
  { value: 'purchase-orders', label: 'Purchase Orders', desc: 'Import POs with vendor and line items' },
  { value: 'sales-orders', label: 'Sales Orders', desc: 'Import SOs with customer and line items' },
];

export default function Imports() {
  const [importType, setImportType] = useState('items');
  const [importResult, setImportResult] = useState(null);
  const [importing, setImporting] = useState(false);
  const fileRef = useRef(null);

  async function handleImport() {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setImportResult(null);
    setImporting(true);

    try {
      const text = await file.text();
      let rows;

      if (file.name.endsWith('.json')) {
        rows = JSON.parse(text);
      } else {
        const lines = text.trim().split('\n');
        const headers = lines[0].split(',').map((h) => h.trim().replace(/^"|"$/g, ''));
        rows = lines.slice(1).map((line) => {
          const vals = line.split(',').map((v) => v.trim().replace(/^"|"$/g, ''));
          const obj = {};
          headers.forEach((h, i) => { obj[h] = vals[i] || ''; });
          return obj;
        });
      }

      const res = await api.post(`/admin/import/${importType}`, { records: rows });
      if (res?.ok) {
        const data = await res.json();
        setImportResult(data);
      } else {
        const data = await res?.json();
        setImportResult({ error: data?.error || 'Import failed' });
      }
    } catch (err) {
      setImportResult({ error: 'Failed to parse file' });
    }
    setImporting(false);
    fileRef.current.value = '';
  }

  return (
    <div>
      <PageHeader title="Import" />

      <div className="settings-section">
        <h3>Import Type</h3>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
          {IMPORT_TYPES.map((t) => (
            <button
              key={t.value}
              className={`btn ${importType === t.value ? 'btn-primary' : ''}`}
              onClick={() => { setImportType(t.value); setImportResult(null); }}
            >
              {t.label}
            </button>
          ))}
        </div>
        <p className="settings-note">{IMPORT_TYPES.find((t) => t.value === importType)?.desc}</p>
      </div>

      <div className="settings-section">
        <h3>Upload File</h3>
        <p className="settings-note" style={{ marginBottom: 12 }}>Upload a CSV or JSON file. Use the template for the correct format.</p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <input ref={fileRef} type="file" accept=".csv,.json" style={{ fontSize: 13 }} />
          <button className="btn btn-primary" onClick={handleImport} disabled={importing}>
            {importing ? 'Importing...' : 'Import'}
          </button>
          <button className="btn btn-sm" onClick={() => downloadTemplate(importType)} style={{ fontSize: 12 }}>
            Download Template
          </button>
        </div>
      </div>

      {importResult && (
        <div className="settings-section">
          <h3>Results</h3>
          <div className="import-results">
            {importResult.error ? (
              <div className="errors">{importResult.error}</div>
            ) : (
              <>
                <div className="success">Imported: {importResult.imported ?? 0}</div>
                {importResult.errors?.length > 0 && (
                  <div className="errors" style={{ marginTop: 4 }}>
                    Errors: {importResult.errors.length}
                    <ul style={{ margin: '4px 0 0 16px', fontSize: 12 }}>
                      {importResult.errors.slice(0, 20).map((err, i) => (
                        <li key={i}>Row {err.row}: {err.error}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
