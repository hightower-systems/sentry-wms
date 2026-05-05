/**
 * #159: Tokens create-modal renders checkbox scope pickers.
 *
 * The v1.5.1 security audit (V-200, V-210) landed the server-side
 * enforcement for token-scope fields. #159 is the paired UX fix:
 * replace the three free-text comma-separated inputs
 * (warehouse_ids, event_types, endpoints) with checkbox lists
 * backed by the existing /admin/warehouses endpoint and the new
 * /admin/scope-catalog endpoint.
 *
 * These tests lock:
 *   - Modal open fetches scope-catalog + warehouses.
 *   - Each scope field renders as a ScopeCheckboxList with All /
 *     None buttons and a "N / M selected" counter.
 *   - All button checks every box for its list.
 *   - Submission sends warehouse_ids: number[], event_types:
 *     string[], endpoints: string[] to POST /admin/tokens.
 *   - The Advanced disclosure reveals the legacy free-text inputs
 *     and toggling off parses their contents back into checkboxes.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiDeleteMock = vi.fn();

vi.mock('../api.js', () => ({
  api: {
    get: (...args) => apiGetMock(...args),
    post: (...args) => apiPostMock(...args),
    put: vi.fn(),
    delete: (...args) => apiDeleteMock(...args),
  },
}));

vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1, warehouse: { warehouse_id: 1 } }),
}));

function jsonResponse(body, ok = true) {
  return Promise.resolve({ ok, status: ok ? 200 : 400, json: () => Promise.resolve(body) });
}

import Tokens from '../pages/Tokens.jsx';

function wireDefaults() {
  apiGetMock.mockImplementation((path) => {
    if (path === '/admin/tokens') return jsonResponse({ tokens: [] });
    if (path === '/admin/scope-catalog') {
      return jsonResponse({
        event_types: [
          'adjustment.applied',
          'cycle_count.adjusted',
          'pack.confirmed',
          'pick.confirmed',
          'receipt.completed',
          'ship.confirmed',
          'transfer.completed',
        ],
        endpoints: [
          'events.ack',
          'events.poll',
          'events.schema',
          'events.types',
          'snapshot.inventory',
        ],
        // v1.7.0 Pipe B additions: inbound resource keys + the
        // operator-managed allowlist of source_systems.
        inbound_resources: [
          'sales_orders', 'items', 'customers', 'vendors', 'purchase_orders',
        ],
        source_systems: [
          { source_system: 'fabric', kind: 'internal_tool' },
          { source_system: 'netsuite', kind: 'connector' },
        ],
      });
    }
    if (path === '/admin/warehouses') {
      return jsonResponse({
        warehouses: [
          { warehouse_id: 1, warehouse_code: 'APT-LAB', warehouse_name: 'Apartment Test Lab' },
          { warehouse_id: 2, warehouse_code: 'VIRTUAL', warehouse_name: 'Virtual Warehouse' },
        ],
      });
    }
    return jsonResponse({}, false);
  });
  apiPostMock.mockResolvedValue({
    ok: true,
    status: 201,
    json: () => Promise.resolve({
      token_id: 42,
      token_name: 'test-token',
      token: 'plaintext-value-shown-exactly-once',
      status: 'active',
      created_at: '2026-04-23T21:00:00Z',
      rotated_at: '2026-04-23T21:00:00Z',
      expires_at: null,
    }),
  });
}

describe('Tokens create-modal checkbox scope pickers (#159)', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiPostMock.mockReset();
    apiDeleteMock.mockReset();
    wireDefaults();
  });

  async function openModal(container) {
    const newBtn = await waitFor(() =>
      within(container).getByRole('button', { name: /new token/i })
    );
    fireEvent.click(newBtn);
    // Scope-catalog + warehouses fire on modal open.
    await waitFor(() => {
      expect(apiGetMock).toHaveBeenCalledWith('/admin/scope-catalog');
      expect(apiGetMock).toHaveBeenCalledWith('/admin/warehouses');
    });
  }

  it('opens modal and fires the two catalog fetches', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);
  });

  it('renders a checkbox for every warehouse, every event_type, every endpoint', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);

    // Warehouse labels render the code in a mono span plus the
    // human-readable name. Use the checkbox role with accessible
    // name to bind to the row without tripping substring-collisions
    // between "VIRTUAL" (code) and "Virtual Warehouse" (name).
    await waitFor(() => {
      expect(within(container).getByRole('checkbox', { name: /APT-LAB/ })).toBeInTheDocument();
    });
    expect(within(container).getByRole('checkbox', { name: /VIRTUAL/ })).toBeInTheDocument();

    // Event-type slugs rendered mono.
    expect(within(container).getByText('receipt.completed')).toBeInTheDocument();
    expect(within(container).getByText('ship.confirmed')).toBeInTheDocument();

    // Endpoint slugs.
    expect(within(container).getByText('events.poll')).toBeInTheDocument();
    expect(within(container).getByText('snapshot.inventory')).toBeInTheDocument();
  });

  it('All button selects every option for its list', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);

    // Four "All" buttons -- one per list. v1.7.0 added inbound_resources
    // alongside warehouses / event_types / endpoints.
    const allBtns = await waitFor(() => {
      const btns = within(container).getAllByRole('button', { name: /^All$/ });
      expect(btns.length).toBe(4);
      return btns;
    });

    allBtns.forEach((b) => fireEvent.click(b));

    // After clicking All on every list, the "N / M selected" counter
    // should read 2 / 2 (warehouses), 7 / 7 (event_types), 5 / 5
    // (endpoints), 5 / 5 (inbound_resources).
    expect(within(container).getByText('2 / 2 selected')).toBeInTheDocument();
    expect(within(container).getByText('7 / 7 selected')).toBeInTheDocument();
    // 5 / 5 appears twice (endpoints + inbound_resources both have 5
    // options). Use queryAllByText to assert at least one instance.
    expect(within(container).queryAllByText('5 / 5 selected').length).toBe(2);
  });

  it('submits the correct array payload', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);

    // Name the token.
    const nameInput = within(container).getByPlaceholderText('fabric-prod');
    fireEvent.change(nameInput, { target: { value: 'submit-probe' } });

    // Check one of each.
    const wh1 = within(container).getByRole('checkbox', { name: /APT-LAB/ });
    fireEvent.click(wh1);
    const et1 = within(container).getByRole('checkbox', { name: /receipt\.completed/ });
    fireEvent.click(et1);
    const ep1 = within(container).getByRole('checkbox', { name: /events\.poll/ });
    fireEvent.click(ep1);

    fireEvent.click(within(container).getByRole('button', { name: /^Create$/ }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1));
    const [path, body] = apiPostMock.mock.calls[0];
    expect(path).toBe('/admin/tokens');
    expect(body).toEqual(expect.objectContaining({
      token_name: 'submit-probe',
      warehouse_ids: [1],
      event_types: ['receipt.completed'],
      endpoints: ['events.poll'],
    }));
    // v1.7.0 fields default to outbound-only when no inbound is selected.
    expect(body.source_system).toBeNull();
    expect(body.inbound_resources).toEqual([]);
    expect(body.mapping_override).toBe(false);
  });

  it('short-circuits with an error when no direction is set', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);
    fireEvent.change(within(container).getByPlaceholderText('fabric-prod'), {
      target: { value: 'no-direction' },
    });
    // Check a warehouse + event type but NO endpoints and NO inbound.
    fireEvent.click(within(container).getByRole('checkbox', { name: /APT-LAB/ }));
    fireEvent.click(within(container).getByRole('checkbox', { name: /receipt\.completed/ }));

    fireEvent.click(within(container).getByRole('button', { name: /^Create$/ }));

    // POST must NOT fire; admin sees an inline error.
    expect(apiPostMock).not.toHaveBeenCalled();
    expect(
      within(container).getByText(/at least one direction is required/i)
    ).toBeInTheDocument();
  });

  it('advanced toggle reveals free-text inputs and submits from them', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);
    fireEvent.change(within(container).getByPlaceholderText('fabric-prod'), {
      target: { value: 'advanced-path' },
    });
    // Flip the advanced toggle.
    const advToggle = within(container).getByRole('checkbox', {
      name: /advanced: paste comma-separated values/i,
    });
    fireEvent.click(advToggle);

    // Now the three free-text inputs appear with placeholders from
    // the pre-v1.5.1 shape. Populate them directly.
    fireEvent.change(within(container).getByPlaceholderText('1, 2'), {
      target: { value: '2' },
    });
    fireEvent.change(
      within(container).getByPlaceholderText('receipt.completed, ship.confirmed'),
      { target: { value: 'ship.confirmed' } },
    );
    fireEvent.change(
      within(container).getByPlaceholderText('events.poll, snapshot.inventory'),
      { target: { value: 'snapshot.inventory, events.poll' } },
    );

    fireEvent.click(within(container).getByRole('button', { name: /^Create$/ }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1));
    const [, body] = apiPostMock.mock.calls[0];
    expect(body).toEqual(expect.objectContaining({
      token_name: 'advanced-path',
      warehouse_ids: [2],
      event_types: ['ship.confirmed'],
      endpoints: ['snapshot.inventory', 'events.poll'],
    }));
  });

  it('toggling advanced on then off hydrates checkboxes from the pasted text', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);

    // Flip advanced on; paste endpoints text; flip advanced off.
    const advToggle = within(container).getByRole('checkbox', {
      name: /advanced: paste comma-separated values/i,
    });
    fireEvent.click(advToggle);
    fireEvent.change(
      within(container).getByPlaceholderText('events.poll, snapshot.inventory'),
      { target: { value: 'events.poll, events.ack' } },
    );
    fireEvent.click(advToggle);

    // The endpoint checkboxes for events.poll + events.ack should now be
    // checked; the others unchecked.
    const pollCbx = within(container).getByRole('checkbox', { name: /events\.poll/ });
    const ackCbx = within(container).getByRole('checkbox', { name: /events\.ack/ });
    const schemaCbx = within(container).getByRole('checkbox', { name: /events\.schema/ });
    expect(pollCbx.checked).toBe(true);
    expect(ackCbx.checked).toBe(true);
    expect(schemaCbx.checked).toBe(false);
  });

  // ---- v1.7.0 Pipe B inbound scope inputs ----

  it('renders the source_system dropdown populated from /admin/scope-catalog', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);
    const select = within(container).getByLabelText(/source system/i);
    const options = Array.from(select.querySelectorAll('option')).map(
      (o) => o.value,
    );
    expect(options).toContain('');
    expect(options).toContain('fabric');
    expect(options).toContain('netsuite');
  });

  it('renders inbound_resources checkboxes for each resource key', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);
    expect(
      within(container).getByRole('checkbox', { name: /sales_orders/ })
    ).toBeInTheDocument();
    expect(
      within(container).getByRole('checkbox', { name: /^items$/ })
    ).toBeInTheDocument();
    expect(
      within(container).getByRole('checkbox', { name: /customers/ })
    ).toBeInTheDocument();
  });

  it('submits an inbound-only token with source_system + inbound_resources', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);
    fireEvent.change(within(container).getByPlaceholderText('fabric-prod'), {
      target: { value: 'inbound-only' },
    });
    // Source system select.
    fireEvent.change(within(container).getByLabelText(/source system/i), {
      target: { value: 'fabric' },
    });
    // Pick one inbound resource.
    fireEvent.click(
      within(container).getByRole('checkbox', { name: /sales_orders/ })
    );
    // No outbound endpoints selected -- should still submit because
    // inbound is set.
    fireEvent.click(within(container).getByRole('button', { name: /^Create$/ }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1));
    const [, body] = apiPostMock.mock.calls[0];
    expect(body).toEqual(expect.objectContaining({
      token_name: 'inbound-only',
      endpoints: [],
      source_system: 'fabric',
      inbound_resources: ['sales_orders'],
      mapping_override: false,
    }));
  });

  it('refuses a half-configured inbound (source_system without inbound_resources)', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);
    fireEvent.change(within(container).getByPlaceholderText('fabric-prod'), {
      target: { value: 'half-configured' },
    });
    fireEvent.change(within(container).getByLabelText(/source system/i), {
      target: { value: 'fabric' },
    });
    // Pick an outbound endpoint so we don't trip the
    // "at least one direction" rule first.
    fireEvent.click(within(container).getByRole('checkbox', { name: /events\.poll/ }));

    fireEvent.click(within(container).getByRole('button', { name: /^Create$/ }));
    expect(apiPostMock).not.toHaveBeenCalled();
    expect(
      within(container).getByText(
        /source system and inbound resources must be set together/i
      )
    ).toBeInTheDocument();
  });

  it('mapping_override flag carried in the submit payload', async () => {
    const { container } = render(
      <MemoryRouter><Tokens /></MemoryRouter>
    );
    await openModal(container);
    fireEvent.change(within(container).getByPlaceholderText('fabric-prod'), {
      target: { value: 'override-token' },
    });
    fireEvent.change(within(container).getByLabelText(/source system/i), {
      target: { value: 'fabric' },
    });
    fireEvent.click(
      within(container).getByRole('checkbox', { name: /customers/ })
    );
    fireEvent.click(
      within(container).getByRole('checkbox', {
        name: /allow mapping_overrides/i,
      })
    );
    fireEvent.click(within(container).getByRole('button', { name: /^Create$/ }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalledTimes(1));
    const [, body] = apiPostMock.mock.calls[0];
    expect(body.mapping_override).toBe(true);
    expect(body.inbound_resources).toContain('customers');
  });
});
