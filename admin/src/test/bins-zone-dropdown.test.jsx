/**
 * v1.4.2 #99: regression lock for the Bin create Zone dropdown.
 *
 * The zone <option> was binding key/value to z.id, but /admin/zones
 * returns zone_id. Every option rendered with an empty value, so no
 * matter which zone the operator picked, the form submitted
 * {zone_id: null} and the backend rejected with int_type.
 *
 * This test:
 *   1. Mocks the admin API so the zones dropdown is populated from a
 *      list that has zone_id fields (never .id).
 *   2. Opens the New Bin modal, fills the required fields, selects a
 *      zone, and clicks Save.
 *   3. Captures the POST body that reaches api.post and asserts
 *      zone_id is the numeric id of the selected zone.
 *
 * Intended to fail against the bug (Bins.jsx:132 using z.id) and pass
 * against the fix (z.zone_id).
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const apiPostMock = vi.fn();
const apiGetMock = vi.fn();

vi.mock('../api.js', () => ({
  api: {
    get: (...args) => apiGetMock(...args),
    post: (...args) => apiPostMock(...args),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1, warehouse: { warehouse_id: 1 } }),
}));

function jsonResponse(body, ok = true) {
  return Promise.resolve({ ok, status: ok ? 200 : 400, json: () => Promise.resolve(body) });
}

import Bins from '../pages/Bins.jsx';

describe('Bin create Zone dropdown (issue #99)', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiPostMock.mockReset();
    apiGetMock.mockImplementation((path) => {
      if (path.startsWith('/admin/bins')) return jsonResponse({ bins: [] });
      if (path.startsWith('/admin/zones')) {
        return jsonResponse({
          zones: [
            { zone_id: 2, zone_code: 'PICK', zone_name: 'Picking' },
            { zone_id: 3, zone_code: 'STORAGE', zone_name: 'Storage' },
          ],
        });
      }
      return jsonResponse({});
    });
    apiPostMock.mockImplementation(() =>
      jsonResponse({ bin_id: 42, bin_code: 'TEST', zone_id: 2 }, true),
    );
  });

  it('submits the selected zone_id as a number, not null or zero', async () => {
    const { findByText, getByRole, getAllByRole } = render(
      <MemoryRouter>
        <Bins />
      </MemoryRouter>,
    );

    // Wait for the initial loads, then open the create modal.
    await waitFor(() => expect(apiGetMock).toHaveBeenCalled());
    fireEvent.click(await findByText('New Bin'));

    // The form has no htmlFor bindings, so target by role. Inputs in
    // render order are Bin Code, Barcode, Aisle; Pick Sequence is a
    // number input (spinbutton). The two selects are Type, then Zone.
    const textboxes = getAllByRole('textbox');
    fireEvent.change(textboxes[0], { target: { value: 'TEST-Z' } }); // Bin Code
    fireEvent.change(textboxes[1], { target: { value: 'TEST-Z' } }); // Barcode

    const selects = getAllByRole('combobox');
    fireEvent.change(selects[0], { target: { value: 'Pickable' } }); // Type
    // Select the second zone (zone_id = 3) via the Zone dropdown.
    fireEvent.change(selects[1], { target: { value: '3' } });

    fireEvent.click(getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalled());

    const [path, body] = apiPostMock.mock.calls[0];
    expect(path).toBe('/admin/bins');
    // The crux of the regression test: zone_id must be the selected
    // numeric id (3), not null (the pre-fix bug) and not 0 (also
    // covered by the saveBin coercion guard).
    expect(typeof body.zone_id).toBe('number');
    expect(body.zone_id).toBe(3);
  });
});
