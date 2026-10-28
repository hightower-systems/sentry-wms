import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api.js', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    postForm: vi.fn(),
    put: vi.fn(),
  },
}));

vi.mock('../warehouse.jsx', () => ({
  useWarehouse: () => ({ warehouseId: 1 }),
}));

import { api } from '../api.js';
import ShipyardOutbound from '../pages/ShipyardOutbound.jsx';

function okJson(data) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(data),
  });
}

const imports = {
  imports: [
    {
      import_id: 'import-1',
      import_no: 'DIST-20261027-ABCD1234',
      source_file_name: '托盘工单.xlsx',
      status: 'PARSED',
      line_count: 6,
      created_at: '2026-10-27T09:00:00+08:00',
    },
  ],
};

const dispatches = {
  dispatches: [
    {
      dispatch_order_id: 'dispatch-1',
      dispatch_no: 'PT202610270001',
      dispatch_date: '2026-10-27',
      status: 'CONFIRMED',
      line_count: 1,
      matched_line_count: 0,
      ambiguous_line_count: 1,
      unmatched_line_count: 0,
    },
  ],
};

const notes = { delivery_notes: [] };
const formalManifests = { manifests: [] };
const formalOrders = { orders: [] };

const formalDetail = {
  manifest: {
    manifest_id: 'formal-1',
    manifest_no: 'OB-20261027-001',
    factory_shipment_no: 'CX-20261027-001',
    source_file_name: '正式出库清单.xlsx',
    status: 'PARSED',
    metadata: { sheet_name: '出库清单' },
  },
  lines: [
    {
      line_id: 'formal-line-1',
      source_row_no: '1',
      ship_no: 'H9301',
      engineering_no: 'H9301',
      production_drawing_no: 'D-001',
      material_code: 'MAT-001',
      item_name: '船用支架',
      item_spec: 'S-100',
      demand_quantity: '4.000',
      allocated_quantity: '4.000',
      shortage_quantity: '0.000',
      unit: '件',
      match_status: 'READY',
      expected_tag_suffixes: ['1-1', '1-2'],
      tasks: [
        {
          ticket_id: 'ticket-1',
          ticket_no: 'T-001',
          available_quantity: '4.000',
          location_path_snapshot: 'A 区 / A1',
          tag_suffixes: ['1-1', '1-2'],
        },
      ],
    },
  ],
  order: null,
  validation: {
    blocking_errors: [],
    summary: {
      line_count: 1,
      task_count: 1,
      shortage_line_count: 0,
      total_shortage_quantity: '0.000',
    },
  },
};

const importDetail = {
  import: {
    import_id: 'import-1',
    import_no: 'DIST-20261027-ABCD1234',
    source_file_name: '托盘工单.xlsx',
    status: 'PARSED',
    metadata: { sheet_name: '托盘工单' },
  },
  lines: [
    {
      line_id: 'line-1',
      source_row_no: '2',
      engineering_no: 'H1613',
      section_no: '101',
      pallet_no: 'P-1',
      pallet_name: '测试托盘',
      demand_window_raw: '2026-10-27 08:00~09:00',
      delivery_status: '未到',
    },
  ],
  warnings: [],
};

const dispatchDetail = {
  dispatch: {
    dispatch_order_id: 'dispatch-1',
    dispatch_no: 'PT202610270001',
    dispatch_date: '2026-10-27',
    source_type: 'IMAGE_OCR',
    source_file_name: 'dispatch.jpg',
    status: 'CONFIRMED',
  },
  lines: [
    {
      dispatch_line_id: 'dispatch-line-1',
      line_no: '1',
      engineering_no: 'H1613',
      pallet_no: 'P-1',
      material_spec_raw: '电舾件',
      specification: '0083-85',
      quantity: '4',
      unit: '件',
      match_status: 'AMBIGUOUS',
      demand_import_no: null,
      demand_source_row_no: null,
      match_details: {
        candidates: [
          {
            line_id: 'line-1',
            import_no: 'DIST-20261027-ABCD1234',
            source_row_no: '2',
            pallet_name: '测试托盘',
          },
        ],
      },
    },
  ],
  match_summary: { matched: 0, ambiguous: 1, unmatched: 0 },
};

async function openDeliveryWorkspace() {
  fireEvent.click(screen.getByRole('tab', { name: /配送与发货/ }));
  await screen.findByText('DIST-20261027-ABCD1234');
}

describe('ShipyardOutbound', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.get).mockImplementation((path) => {
      if (path.endsWith('/formal-manifests/formal-1')) {
        return okJson(formalDetail);
      }
      if (path.endsWith('/distribution-imports/import-1')) {
        return okJson(importDetail);
      }
      if (path.endsWith('/dispatches/dispatch-1')) {
        return okJson(dispatchDetail);
      }
      if (path.includes('distribution-imports')) return okJson(imports);
      if (path.includes('dispatches')) return okJson(dispatches);
      if (path.includes('delivery-notes')) return okJson(notes);
      if (path.includes('formal-manifests')) return okJson(formalManifests);
      if (path.includes('/outbound/orders')) return okJson(formalOrders);
      return okJson({});
    });
  });

  it('loads only the formal queue first and opens delivery on demand', async () => {
    render(<ShipyardOutbound />);

    expect(await screen.findByText('还没有正式出库清单')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith(
      '/shipyard/outbound/formal-manifests?warehouse_id=1',
    );
    expect(api.get).toHaveBeenCalledWith(
      '/shipyard/outbound/orders?warehouse_id=1',
    );
    expect(api.get).not.toHaveBeenCalledWith(
      '/shipyard/outbound/distribution-imports?warehouse_id=1',
    );

    await openDeliveryWorkspace();
    expect(await screen.findByText('DIST-20261027-ABCD1234')).toBeInTheDocument();
    expect(screen.getByText('PT202610270001')).toBeInTheDocument();
    expect(screen.getByText('还没有发货单')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith(
      '/shipyard/outbound/distribution-imports?warehouse_id=1',
    );
    expect(api.get).toHaveBeenCalledWith(
      '/shipyard/outbound/dispatches?warehouse_id=1',
    );
  });

  it('uploads the selected demand workbook as multipart data', async () => {
    vi.mocked(api.postForm).mockResolvedValue({
      ok: true,
      status: 201,
      json: () => Promise.resolve({
        import: {
          import_id: 'import-2',
          import_no: 'DIST-NEW',
          status: 'PARSED',
          metadata: { sheet_name: '托盘工单' },
        },
        lines: [
          {
            line_id: 'line-1',
            source_row_no: '2',
            engineering_no: 'H1613',
            section_no: '101',
            pallet_no: 'P-1',
            pallet_name: '测试托盘',
            demand_window_raw: '2026-10-27 08:00~09:00',
            delivery_status: '未到',
          },
        ],
        warnings: [],
      }),
    });
    const { container } = render(<ShipyardOutbound />);
    await screen.findByText('导入正式出库清单');
    await openDeliveryWorkspace();

    const input = container.querySelector('#shipyard-demand-file');
    fireEvent.change(input, {
      target: {
        files: [
          new File(['xlsx'], '船厂配送.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          }),
        ],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: '识别并保存' }));

    await waitFor(() => expect(api.postForm).toHaveBeenCalledTimes(1));
    const [path, form] = vi.mocked(api.postForm).mock.calls[0];
    expect(path).toBe('/shipyard/outbound/distribution-imports/preview');
    expect(form.get('warehouse_id')).toBe('1');
    expect(form.get('file').name).toBe('船厂配送.xlsx');
    expect(await screen.findByText('配送需求预览 · DIST-NEW')).toBeInTheDocument();
  });

  it('uploads a formal outbound workbook and shows material identity', async () => {
    vi.mocked(api.postForm).mockResolvedValue({
      ok: true,
      status: 201,
      json: () => Promise.resolve(formalDetail),
    });
    const { container } = render(<ShipyardOutbound />);
    await screen.findByText('导入正式出库清单');

    const input = container.querySelector('#shipyard-formal-outbound-file');
    fireEvent.change(input, {
      target: {
        files: [
          new File(['xlsx'], 'formal.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          }),
        ],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: '识别并匹配库存' }));

    await waitFor(() => {
      expect(api.postForm).toHaveBeenCalledWith(
        '/shipyard/outbound/formal-manifests/preview',
        expect.any(FormData),
      );
    });
    expect(await screen.findByText('MAT-001')).toBeInTheDocument();
    expect(screen.getByText('A 区 / A1')).toBeInTheDocument();
  });

  it('confirms a parsed import only after opening its detail', async () => {
    vi.mocked(api.post).mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        ...importDetail,
        import: { ...importDetail.import, status: 'CONFIRMED' },
      }),
    });
    render(<ShipyardOutbound />);
    await screen.findByText('导入正式出库清单');
    await openDeliveryWorkspace();

    fireEvent.click(screen.getByRole('button', { name: '查看' }));
    expect(await screen.findByText('配送需求预览 · DIST-20261027-ABCD1234'))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认导入' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/shipyard/outbound/distribution-imports/import-1/confirm',
      {},
    ));
    expect(await screen.findByText('配送需求已确认，可参与派工单匹配'))
      .toBeInTheDocument();
  });

  it('keeps the generated order visible after confirming a formal manifest', async () => {
    vi.mocked(api.postForm).mockResolvedValue({
      ok: true,
      status: 201,
      json: () => Promise.resolve(formalDetail),
    });
    vi.mocked(api.post).mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        order: {
          outbound_order_id: 'formal-order-1',
          outbound_order_no: 'OUT-FORMAL-1',
          factory_shipment_no: 'CX-20261027-001',
          status: 'PENDING',
        },
        summary: {
          task_count: 1,
          pending_task_count: 1,
          outbound_task_count: 0,
          shortage_line_count: 0,
        },
        tasks: [],
      }),
    });
    const { container } = render(<ShipyardOutbound />);
    await screen.findByText('导入正式出库清单');
    fireEvent.change(container.querySelector('#shipyard-formal-outbound-file'), {
      target: {
        files: [
          new File(['xlsx'], 'formal.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          }),
        ],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: '识别并匹配库存' }));
    fireEvent.click(await screen.findByRole('button', {
      name: '确认并生成正式出库单',
    }));

    expect(await screen.findByText('正式出库单 · OUT-FORMAL-1'))
      .toBeInTheDocument();
  });

  it('manually resolves an ambiguous demand and creates a delivery note', async () => {
    vi.mocked(api.put).mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        ...dispatchDetail,
        lines: [{
          ...dispatchDetail.lines[0],
          match_status: 'MANUAL',
          demand_import_no: 'DIST-20261027-ABCD1234',
          demand_source_row_no: '2',
        }],
      }),
    });
    vi.mocked(api.post).mockImplementation((path, body) => {
      if (path.endsWith('/delivery-note')) {
        return Promise.resolve({
          ok: true,
          status: 201,
          json: () => Promise.resolve({
            delivery_note: {
              delivery_note_id: 'note-1',
              delivery_note_no: body.delivery_note_no,
              dispatch_no: 'PT202610270001',
              issue_date: body.issue_date,
            },
            lines: [
              {
                delivery_line_id: 'delivery-line-1',
                line_no: '1',
                engineering_no: 'H1613',
                section_no: '101',
                pallet_no: 'P-1',
                material_description: '电舾件',
                specification: '0083-85',
                quantity: '4',
                unit: '件',
                demand_import_no: 'DIST-20261027-ABCD1234',
                demand_source_row_no: '2',
              },
            ],
          }),
        });
      }
      return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) });
    });
    render(<ShipyardOutbound />);
    await screen.findByText('导入正式出库清单');
    await openDeliveryWorkspace();
    await screen.findByText('PT202610270001');
    fireEvent.click(screen.getByRole('button', { name: '处理' }));

    const candidate = await screen.findByLabelText('派工行 1 候选需求');
    fireEvent.change(candidate, { target: { value: 'line-1' } });
    fireEvent.click(screen.getByRole('button', { name: '匹配' }));
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(
      '/shipyard/outbound/dispatch-lines/dispatch-line-1/demand-match',
      { demand_line_id: 'line-1' },
    ));

    fireEvent.change(screen.getByLabelText('发货单号'), {
      target: { value: 'FH-TEST-001' },
    });
    fireEvent.click(screen.getByRole('button', { name: '生成发货单' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/shipyard/outbound/dispatches/dispatch-1/delivery-note',
      expect.objectContaining({
        delivery_note_no: 'FH-TEST-001',
        issue_date: expect.any(String),
        idempotency_key: expect.stringContaining('delivery-note-'),
      }),
    ));
    expect(await screen.findByText('发货单 · FH-TEST-001')).toBeInTheDocument();
  });

  it('does not offer confirmation while the formal preview has blocking errors', async () => {
    vi.mocked(api.postForm).mockResolvedValue({
      ok: true,
      status: 201,
      json: () => Promise.resolve({
        ...formalDetail,
        validation: {
          ...formalDetail.validation,
          blocking_errors: [{
            code: 'BINDING_CONFIGURATION_REQUIRED',
            message: '项目绑定配置缺失',
          }],
        },
      }),
    });
    const { container } = render(<ShipyardOutbound />);
    await screen.findByText('导入正式出库清单');

    fireEvent.change(container.querySelector('#shipyard-formal-outbound-file'), {
      target: {
        files: [
          new File(['xlsx'], 'blocked.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          }),
        ],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: '识别并匹配库存' }));

    expect(await screen.findByText('BINDING_CONFIGURATION_REQUIRED')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '确认并生成正式出库单' }),
    ).toBeDisabled();
    expect(screen.getByRole('link', { name: '去处理船号与区域绑定' }))
      .toHaveAttribute('href', '/shipyard-settings');
  });

  it('refreshes an open outbound order after field work changes its status', async () => {
    let fieldWorkComplete = false;
    const pendingOrder = {
      outbound_order_id: 'order-live',
      outbound_order_no: 'OUT-LIVE',
      factory_shipment_no: 'CX-LIVE',
      line_count: 1,
      task_count: 1,
      pending_task_count: 1,
      total_required_quantity: '4.000',
      total_fulfilled_quantity: '0.000',
      status: 'PENDING',
    };
    const orderDetail = () => ({
      order: {
        outbound_order_id: 'order-live',
        outbound_order_no: 'OUT-LIVE',
        factory_shipment_no: 'CX-LIVE',
        status: fieldWorkComplete ? 'LOADED' : 'PENDING',
      },
      summary: {
        pending_task_count: fieldWorkComplete ? 0 : 1,
        outbound_task_count: fieldWorkComplete ? 1 : 0,
      },
      tasks: [{
        task_id: 'task-live',
        task_no: 'TASK-LIVE',
        source_row_no: '1',
        material_code: 'MAT-LIVE',
        item_name: '现场物料',
        item_spec: 'S-1',
        ticket_no: 'TICKET-LIVE',
        location_path_snapshot: 'A 区 / A1',
        planned_quantity: '4.000',
        expected_tag_suffixes: ['1-1'],
        status: fieldWorkComplete ? 'OUTBOUND' : 'PENDING',
        confirmation: fieldWorkComplete
          ? { outbound_by: 'field-operator', outbound_at: '2026-10-27T12:00:00+08:00' }
          : null,
      }],
    });
    vi.mocked(api.get).mockImplementation((requestPath) => {
      if (requestPath.includes('/outbound/orders/order-live')) {
        return okJson(orderDetail());
      }
      if (requestPath.includes('formal-manifests')) return okJson(formalManifests);
      if (requestPath.includes('/outbound/orders')) {
        return okJson({ orders: [{ ...pendingOrder }] });
      }
      return okJson({});
    });

    render(<ShipyardOutbound />);
    fireEvent.click(await screen.findByRole('button', { name: '查看任务' }));
    expect(await screen.findByText('正式出库单 · OUT-LIVE')).toBeInTheDocument();

    fieldWorkComplete = true;
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));

    expect(await screen.findByText(/field-operator/)).toBeInTheDocument();
  });
});
