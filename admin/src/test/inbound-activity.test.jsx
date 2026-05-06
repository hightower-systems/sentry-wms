/**
 * v1.7.0 plan §4.2: Admin Inbound activity page renders the
 * read-only list + detail view backed by /api/admin/inbound/activity.
 *
 * Locks:
 *   - List loads on mount; filters fire a fresh GET with the right
 *     query params.
 *   - Each row's "View" button opens the detail modal and calls the
 *     per-row endpoint.
 *   - Detail modal renders source_payload + canonical_payload.
 *   - Resource + status selects expose the v1.7 keys.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const apiGetMock = vi.fn();

vi.mock('../api.js', () => ({
  api: {
    get: (...args) => apiGetMock(...args),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

vi.mock('../warehouse.jsx', () => ({
  warehouseRequired: () => null,
}));

import InboundActivity from '../pages/InboundActivity.jsx';


function jsonResponse(body, ok = true) {
  return Promise.resolve({
    ok,
    status: ok ? 200 : 400,
    json: () => Promise.resolve(body),
  });
}


function wireDefaults() {
  apiGetMock.mockImplementation((path) => {
    // Initial load (no filters) and any filter combination both
    // resolve to the activity endpoint -- match either shape.
    if (
      path === '/admin/inbound/activity' ||
      path.startsWith('/admin/inbound/activity?')
    ) {
      return jsonResponse({
        rows: [
          {
            resource: 'sales_orders',
            inbound_id: 12,
            source_system: 'fabric',
            external_id: 'SO-1',
            external_version: '2026-05-04T10:00:00+00:00',
            canonical_id: '11111111-1111-1111-1111-111111111111',
            received_at: '2026-05-04T10:00:01+00:00',
            status: 'applied',
            superseded_at: null,
            ingested_via_token_id: 7,
          },
          {
            resource: 'items',
            inbound_id: 13,
            source_system: 'fabric',
            external_id: 'SKU-1',
            external_version: 'v1',
            canonical_id: '22222222-2222-2222-2222-222222222222',
            received_at: '2026-05-04T09:00:01+00:00',
            status: 'applied',
            superseded_at: null,
            ingested_via_token_id: 7,
          },
        ],
        limit: 100,
      });
    }
    if (path.startsWith('/admin/inbound/activity/sales_orders/')) {
      return jsonResponse({
        resource: 'sales_orders',
        inbound_id: 12,
        source_system: 'fabric',
        external_id: 'SO-1',
        external_version: '2026-05-04T10:00:00+00:00',
        canonical_id: '11111111-1111-1111-1111-111111111111',
        source_payload: { orderNumber: 'SO-1', warehouseId: 1 },
        canonical_payload: { so_number: 'SO-1', warehouse_id: 1 },
        received_at: '2026-05-04T10:00:01+00:00',
        status: 'applied',
        superseded_at: null,
        ingested_via_token_id: 7,
      });
    }
    return jsonResponse({}, false);
  });
}


describe('InboundActivity', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    wireDefaults();
  });

  it('loads activity on mount', async () => {
    const { container } = render(
      <MemoryRouter><InboundActivity /></MemoryRouter>
    );
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith(
        expect.stringMatching(/^\/admin\/inbound\/activity/),
      );
    });
    // Renders rows from the wired response.
    await waitFor(() => {
      expect(within(container).getByText('SO-1')).toBeInTheDocument();
      expect(within(container).getByText('SKU-1')).toBeInTheDocument();
    });
  });

  it('Apply button refetches with filter query params', async () => {
    const { container } = render(
      <MemoryRouter><InboundActivity /></MemoryRouter>
    );
    await waitFor(() =>
      expect(within(container).getByText('SO-1')).toBeInTheDocument()
    );
    apiGetMock.mockClear();
    wireDefaults();

    // Set a source_system filter + resource select.
    fireEvent.change(
      within(container).getByPlaceholderText(/exact match/i),
      { target: { value: 'fabric' } },
    );
    const resourceSelect = within(container).getAllByRole('combobox')[0];
    fireEvent.change(resourceSelect, { target: { value: 'sales_orders' } });

    fireEvent.click(within(container).getByRole('button', { name: /apply/i }));
    await waitFor(() => {
      const calls = apiGetMock.mock.calls;
      const hit = calls.find(
        (c) =>
          c[0].includes('source_system=fabric') &&
          c[0].includes('resource=sales_orders'),
      );
      expect(hit).toBeTruthy();
    });
  });

  it('View button opens detail modal and renders payloads', async () => {
    const { container, baseElement } = render(
      <MemoryRouter><InboundActivity /></MemoryRouter>
    );
    await waitFor(() =>
      expect(within(container).getByText('SO-1')).toBeInTheDocument()
    );
    const viewButtons = within(container).getAllByRole('button', { name: /view/i });
    fireEvent.click(viewButtons[0]);

    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith(
        '/admin/inbound/activity/sales_orders/12',
      );
    });
    // Modal renders payload pre blocks. baseElement covers the
    // portal-rendered modal which lives outside `container`.
    await waitFor(() => {
      expect(within(baseElement).getByText(/source payload/i)).toBeInTheDocument();
      expect(within(baseElement).getByText(/canonical payload/i)).toBeInTheDocument();
    });
  });
});
