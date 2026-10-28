import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import PageHeader from '../components/PageHeader.jsx';
import { useWarehouse } from '../warehouse.jsx';

const IMPORT_STATUS = {
  PARSED: '待确认',
  CONFIRMED: '已确认',
  VOID: '已作废',
};

const MATCH_STATUS = {
  MATCHED: '已自动匹配',
  MANUAL: '已人工匹配',
  AMBIGUOUS: '待选需求',
  UNMATCHED: '未匹配',
};

const DISPATCH_STATUS = {
  CONFIRMED: '有效',
  CANCELLED: '已取消',
};

const FORMAL_STATUS = {
  PARSED: '待确认',
  CONFIRMED: '已生成出库单',
  VOID: '已作废',
  PENDING: '待出库',
  IN_PROGRESS: '出库中',
  PARTIAL: '部分出库',
  OUTBOUND: '已出库',
  LOADED: '已装车',
  SHORTAGE: '库存不足',
  READY: '可生成',
  ALLOCATED: '已分配',
};

function todayIso() {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

function randomId(prefix) {
  const suffix = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

function defaultNoteNumber() {
  return `FH-${todayIso().replaceAll('-', '')}-${String(Date.now()).slice(-4)}`;
}

function formatTime(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString('zh-CN', { hour12: false });
}

async function requireJson(response, fallback) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || data.error || fallback);
  }
  return data;
}

async function loadFormalWorkspace(warehouseId) {
  const responses = await Promise.all([
    api.get(`/shipyard/outbound/formal-manifests?warehouse_id=${warehouseId}`),
    api.get(`/shipyard/outbound/orders?warehouse_id=${warehouseId}`),
  ]);
  const [
    formalManifestData,
    formalOrderData,
  ] = await Promise.all([
    requireJson(responses[0], '无法读取正式出库清单'),
    requireJson(responses[1], '无法读取正式出库单'),
  ]);
  return {
    formalManifests: formalManifestData.manifests || [],
    formalOrders: formalOrderData.orders || [],
  };
}

async function loadDeliveryWorkspace(warehouseId) {
  const responses = await Promise.all([
    api.get(`/shipyard/outbound/distribution-imports?warehouse_id=${warehouseId}`),
    api.get(`/shipyard/outbound/dispatches?warehouse_id=${warehouseId}`),
    api.get(`/shipyard/outbound/delivery-notes?warehouse_id=${warehouseId}`),
  ]);
  const [
    importsData,
    dispatchData,
    notesData,
  ] = await Promise.all([
    requireJson(responses[0], '无法读取配送需求'),
    requireJson(responses[1], '无法读取派工单'),
    requireJson(responses[2], '无法读取发货单'),
  ]);
  return {
    imports: importsData.imports || [],
    dispatches: dispatchData.dispatches || [],
    deliveryNotes: notesData.delivery_notes || [],
  };
}

function Status({ value, labels }) {
  return (
    <span className={`shipyard-status shipyard-status-${String(value).toLowerCase()}`}>
      {labels[value] || value || '—'}
    </span>
  );
}

export default function ShipyardOutbound() {
  const { warehouseId } = useWarehouse();
  const fileRef = useRef(null);
  const formalFileRef = useRef(null);
  const formalPreviewRef = useRef(null);
  const formalOrderRef = useRef(null);
  const importPreviewRef = useRef(null);
  const dispatchPreviewRef = useRef(null);
  const deliveryNoteRef = useRef(null);
  const [activeWorkspace, setActiveWorkspace] = useState('formal');
  const [deliveryLoaded, setDeliveryLoaded] = useState(false);
  const [formalManifests, setFormalManifests] = useState([]);
  const [formalOrders, setFormalOrders] = useState([]);
  const [selectedFormal, setSelectedFormal] = useState(null);
  const [selectedFormalOrder, setSelectedFormalOrder] = useState(null);
  const [selectedFormalFile, setSelectedFormalFile] = useState(null);
  const [formalDragging, setFormalDragging] = useState(false);
  const [factoryShipmentNo, setFactoryShipmentNo] = useState('');
  const [imports, setImports] = useState([]);
  const [dispatches, setDispatches] = useState([]);
  const [deliveryNotes, setDeliveryNotes] = useState([]);
  const [selectedImport, setSelectedImport] = useState(null);
  const [selectedDispatch, setSelectedDispatch] = useState(null);
  const [selectedNote, setSelectedNote] = useState(null);
  const [selectedFile, setSelectedFile] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState(null);
  const [voidReason, setVoidReason] = useState('');
  const [noteNo, setNoteNo] = useState(defaultNoteNumber);
  const [issueDate, setIssueDate] = useState(todayIso);
  const [manualDemandIds, setManualDemandIds] = useState({});

  const notesByDispatch = useMemo(
    () => new Map(deliveryNotes.map((note) => [note.dispatch_order_id, note])),
    [deliveryNotes],
  );
  const selectedFormalOrderId = selectedFormalOrder?.order?.outbound_order_id;
  const selectedFormalManifestId = selectedFormal?.manifest?.manifest_id;
  const selectedImportId = selectedImport?.import?.import_id;
  const selectedDispatchId = selectedDispatch?.dispatch?.dispatch_order_id;
  const selectedNoteId = selectedNote?.delivery_note?.delivery_note_id;

  const refreshFormalWorkspace = useCallback(async () => {
    if (!warehouseId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const data = await loadFormalWorkspace(warehouseId);
      setFormalManifests(data.formalManifests);
      setFormalOrders(data.formalOrders);
      if (selectedFormalOrderId) {
        const detailResponse = await api.get(
          `/shipyard/outbound/orders/${selectedFormalOrderId}`,
        );
        setSelectedFormalOrder(
          await requireJson(detailResponse, '无法读取出库任务'),
        );
      }
      if (selectedFormalManifestId) {
        const detailResponse = await api.get(
          `/shipyard/outbound/formal-manifests/${selectedFormalManifestId}`,
        );
        setSelectedFormal(
          await requireJson(detailResponse, '无法读取正式出库清单'),
        );
      }
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setLoading(false);
    }
  }, [warehouseId, selectedFormalManifestId, selectedFormalOrderId]);

  const refreshDeliveryWorkspace = useCallback(async () => {
    if (!warehouseId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const data = await loadDeliveryWorkspace(warehouseId);
      setImports(data.imports);
      setDispatches(data.dispatches);
      setDeliveryNotes(data.deliveryNotes);
      setDeliveryLoaded(true);
      if (selectedImportId) {
        const detailResponse = await api.get(
          `/shipyard/outbound/distribution-imports/${selectedImportId}`,
        );
        setSelectedImport(
          await requireJson(detailResponse, '无法读取配送需求详情'),
        );
      }
      if (selectedDispatchId) {
        const detailResponse = await api.get(
          `/shipyard/outbound/dispatches/${selectedDispatchId}`,
        );
        setSelectedDispatch(
          await requireJson(detailResponse, '无法读取派工单详情'),
        );
      }
      if (selectedNoteId) {
        const detailResponse = await api.get(
          `/shipyard/outbound/delivery-notes/${selectedNoteId}`,
        );
        setSelectedNote(
          await requireJson(detailResponse, '无法读取发货单详情'),
        );
      }
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setLoading(false);
    }
  }, [
    warehouseId,
    selectedDispatchId,
    selectedImportId,
    selectedNoteId,
  ]);

  useEffect(() => {
    if (!warehouseId) return undefined;
    let cancelled = false;
    loadFormalWorkspace(warehouseId)
      .then((data) => {
        if (cancelled) return;
        setFormalManifests(data.formalManifests);
        setFormalOrders(data.formalOrders);
      })
      .catch((error) => {
        if (!cancelled) setNotice({ type: 'error', message: error.message });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [warehouseId]);

  useEffect(() => {
    if (!selectedFormalManifestId) return;
    formalPreviewRef.current?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'start',
    });
  }, [selectedFormalManifestId]);

  useEffect(() => {
    if (!selectedFormalOrderId) return;
    formalOrderRef.current?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'start',
    });
  }, [selectedFormalOrderId]);

  useEffect(() => {
    if (!selectedImportId) return;
    importPreviewRef.current?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'start',
    });
  }, [selectedImportId]);

  useEffect(() => {
    if (!selectedDispatchId) return;
    dispatchPreviewRef.current?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'start',
    });
  }, [selectedDispatchId]);

  useEffect(() => {
    if (!selectedNoteId) return;
    deliveryNoteRef.current?.scrollIntoView?.({
      behavior: 'smooth',
      block: 'start',
    });
  }, [selectedNoteId]);

  async function switchWorkspace(nextWorkspace) {
    setActiveWorkspace(nextWorkspace);
    setNotice(null);
    if (nextWorkspace === 'delivery' && !deliveryLoaded) {
      await refreshDeliveryWorkspace();
    }
  }

  async function refreshActiveWorkspace() {
    if (activeWorkspace === 'formal') {
      await refreshFormalWorkspace();
      return;
    }
    await refreshDeliveryWorkspace();
  }

  function acceptFormalFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      setNotice({ type: 'error', message: '首期只接受船厂下发的 .xlsx 正式出库清单' });
      return;
    }
    setSelectedFormalFile(file);
    setNotice(null);
  }

  async function uploadFormalManifest() {
    if (!warehouseId || !selectedFormalFile) return;
    setBusy('formal-upload');
    setNotice(null);
    try {
      const form = new FormData();
      form.append('warehouse_id', String(warehouseId));
      form.append('file', selectedFormalFile);
      if (factoryShipmentNo.trim()) {
        form.append('factory_shipment_no', factoryShipmentNo.trim());
      }
      const response = await api.postForm(
        '/shipyard/outbound/formal-manifests/preview',
        form,
      );
      const data = await requireJson(response, '正式出库清单识别失败');
      setSelectedFormal(data);
      setSelectedFormalOrder(null);
      setSelectedFormalFile(null);
      setFactoryShipmentNo('');
      if (formalFileRef.current) formalFileRef.current.value = '';
      const summary = data.validation?.summary || {};
      setNotice({
        type: summary.shortage_line_count ? 'warning' : 'success',
        message: `已识别 ${summary.line_count || 0} 行，匹配 ${summary.task_count || 0} 张整票任务${summary.shortage_line_count ? `，${summary.shortage_line_count} 行库存不足` : ''}`,
      });
      await refreshFormalWorkspace();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function openFormalManifest(manifestId) {
    setBusy(`formal-${manifestId}`);
    setSelectedFormalOrder(null);
    try {
      const response = await api.get(
        `/shipyard/outbound/formal-manifests/${manifestId}`,
      );
      setSelectedFormal(await requireJson(response, '无法读取正式出库清单'));
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function confirmFormalManifest() {
    if (!selectedFormal) return;
    if (selectedFormal.validation?.blocking_errors?.length) {
      setNotice({
        type: 'error',
        message: '仍有阻断项，请先处理清单中的绑定或字段错误',
      });
      return;
    }
    const manifestId = selectedFormal.manifest.manifest_id;
    setBusy('formal-confirm');
    try {
      const response = await api.post(
        `/shipyard/outbound/formal-manifests/${manifestId}/confirm`,
        {},
      );
      const order = await requireJson(response, '生成正式出库单失败');
      setSelectedFormalOrder(order);
      setNotice({
        type: order.summary?.shortage_line_count ? 'warning' : 'success',
        message: `正式出库单 ${order.order?.outbound_order_no || ''} 已生成，共 ${order.summary?.task_count || 0} 张整票任务`,
      });
      await refreshFormalWorkspace();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function openFormalOrder(orderId) {
    setBusy(`formal-order-${orderId}`);
    setSelectedFormal(null);
    try {
      const response = await api.get(`/shipyard/outbound/orders/${orderId}`);
      setSelectedFormalOrder(
        await requireJson(response, '无法读取出库任务'),
      );
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  function acceptFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      setNotice({ type: 'error', message: '首期只接受船厂下发的 .xlsx 配送需求表' });
      return;
    }
    setSelectedFile(file);
    setNotice(null);
  }

  async function uploadDemand() {
    if (!warehouseId || !selectedFile) return;
    setBusy('upload');
    setNotice(null);
    try {
      const form = new FormData();
      form.append('warehouse_id', String(warehouseId));
      form.append('file', selectedFile);
      const response = await api.postForm(
        '/shipyard/outbound/distribution-imports/preview',
        form,
      );
      const data = await requireJson(response, '配送需求识别失败');
      setSelectedImport(data);
      setSelectedFile(null);
      if (fileRef.current) fileRef.current.value = '';
      const warningCount = data.warnings?.length || 0;
      setNotice({
        type: warningCount ? 'warning' : 'success',
        message: `已识别 ${data.lines?.length || 0} 票配送需求并保存原文件${warningCount ? `，有 ${warningCount} 条识别提醒需要确认` : ''}`,
      });
      await refreshDeliveryWorkspace();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function openImport(importId) {
    setBusy(`import-${importId}`);
    try {
      const response = await api.get(
        `/shipyard/outbound/distribution-imports/${importId}`,
      );
      setSelectedImport(await requireJson(response, '无法读取配送需求详情'));
      setVoidReason('');
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function confirmImport() {
    if (!selectedImport) return;
    const importId = selectedImport.import.import_id;
    setBusy('confirm-import');
    try {
      const response = await api.post(
        `/shipyard/outbound/distribution-imports/${importId}/confirm`,
        {},
      );
      setSelectedImport(await requireJson(response, '确认配送需求失败'));
      setNotice({ type: 'success', message: '配送需求已确认，可参与派工单匹配' });
      await refreshDeliveryWorkspace();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function voidImport() {
    if (!selectedImport || !voidReason.trim()) {
      setNotice({ type: 'error', message: '作废前请填写原因' });
      return;
    }
    const importId = selectedImport.import.import_id;
    setBusy('void-import');
    try {
      const response = await api.post(
        `/shipyard/outbound/distribution-imports/${importId}/void`,
        { reason: voidReason.trim() },
      );
      setSelectedImport(await requireJson(response, '作废配送需求失败'));
      setNotice({ type: 'success', message: '配送需求已作废，可重新导入同一文件' });
      await refreshDeliveryWorkspace();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function openDispatch(dispatchId) {
    setBusy(`dispatch-${dispatchId}`);
    setSelectedNote(null);
    try {
      const response = await api.get(`/shipyard/outbound/dispatches/${dispatchId}`);
      setSelectedDispatch(await requireJson(response, '无法读取派工单详情'));
      setNoteNo(defaultNoteNumber());
      setIssueDate(todayIso());
      setManualDemandIds({});
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function matchLine(line) {
    const demandLineId = manualDemandIds[line.dispatch_line_id];
    if (!selectedDispatch || !demandLineId) {
      setNotice({ type: 'error', message: '请先选择一条候选配送需求' });
      return;
    }
    setBusy(`match-${line.dispatch_line_id}`);
    try {
      const response = await api.put(
        `/shipyard/outbound/dispatch-lines/${line.dispatch_line_id}/demand-match`,
        { demand_line_id: demandLineId },
      );
      setSelectedDispatch(await requireJson(response, '人工匹配失败'));
      setNotice({ type: 'success', message: `派工行 ${line.line_no} 已人工匹配` });
      await refreshDeliveryWorkspace();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function createDeliveryNote() {
    if (!selectedDispatch) return;
    if (!noteNo.trim()) {
      setNotice({ type: 'error', message: '请填写发货单号' });
      return;
    }
    const dispatchId = selectedDispatch.dispatch.dispatch_order_id;
    setBusy('create-note');
    try {
      const response = await api.post(
        `/shipyard/outbound/dispatches/${dispatchId}/delivery-note`,
        {
          delivery_note_no: noteNo.trim(),
          issue_date: issueDate,
          idempotency_key: randomId('delivery-note'),
        },
      );
      const data = await requireJson(response, '生成发货单失败');
      setSelectedNote(data);
      setNotice({ type: 'success', message: '发货单已生成，可打开打印视图' });
      await refreshDeliveryWorkspace();
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  async function openDeliveryNote(noteId) {
    setBusy(`note-${noteId}`);
    try {
      const response = await api.get(`/shipyard/outbound/delivery-notes/${noteId}`);
      setSelectedNote(await requireJson(response, '无法读取发货单详情'));
    } catch (error) {
      setNotice({ type: 'error', message: error.message });
    } finally {
      setBusy('');
    }
  }

  if (!warehouseId) {
    return (
      <div>
        <PageHeader title="船厂出库单据" />
        <div className="settings-section">请先在顶部选择仓库。</div>
      </div>
    );
  }

  return (
    <div className="shipyard-page">
      <PageHeader title="船厂出库">
        <button
          className="btn"
          onClick={refreshActiveWorkspace}
          disabled={loading || !!busy}
        >
          {loading ? '读取中…' : '刷新'}
        </button>
      </PageHeader>

      {notice && (
        <div className={`shipyard-notice shipyard-notice-${notice.type}`} role="status">
          <span>{notice.message}</span>
          <button className="btn btn-sm" onClick={() => setNotice(null)}>关闭</button>
        </div>
      )}

      <div className="shipyard-workspace-switch" role="tablist" aria-label="船厂出库业务">
        <button
          type="button"
          role="tab"
          aria-selected={activeWorkspace === 'formal'}
          className={activeWorkspace === 'formal' ? 'is-active' : ''}
          onClick={() => switchWorkspace('formal')}
        >
          <strong>正式出库</strong>
          <span>导入、核对、整票出库与装车</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeWorkspace === 'delivery'}
          className={activeWorkspace === 'delivery' ? 'is-active' : ''}
          onClick={() => switchWorkspace('delivery')}
        >
          <strong>配送与发货</strong>
          <span>配送需求、派工匹配与前置仓发货单</span>
        </button>
      </div>

      {activeWorkspace === 'formal' ? (
        <>
      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>导入正式出库清单</h3>
            <p className="settings-note">
              按物料编码、品名、规格、船号/工程号/制作图和整票托盘匹配库存。
              物料编码或品名不一致时只显示缺口，不按名称猜测扣库。
            </p>
          </div>
        </div>
        <div className="shipyard-actions shipyard-formal-meta">
          <label>
            <span>船厂出货单号（文件未提供时填写）</span>
            <input
              className="input"
              value={factoryShipmentNo}
              onChange={(event) => setFactoryShipmentNo(event.target.value)}
              placeholder="例如 CX-20261027-001"
            />
          </label>
        </div>
        <div
          className={`shipyard-dropzone${formalDragging ? ' is-dragging' : ''}`}
          onDragEnter={(event) => {
            event.preventDefault();
            setFormalDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            event.preventDefault();
            setFormalDragging(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            setFormalDragging(false);
            acceptFormalFile(event.dataTransfer.files?.[0]);
          }}
        >
          <input
            ref={formalFileRef}
            id="shipyard-formal-outbound-file"
            type="file"
            accept=".xlsx"
            onChange={(event) => acceptFormalFile(event.target.files?.[0])}
          />
          <label htmlFor="shipyard-formal-outbound-file">
            <strong>
              {selectedFormalFile
                ? selectedFormalFile.name
                : '将船厂正式出库清单拖到这里，或点击选择文件'}
            </strong>
            <span>固定模板 .xlsx；物料编码和品名为强制字段</span>
          </label>
          <button
            className="btn btn-primary"
            onClick={uploadFormalManifest}
            disabled={!selectedFormalFile || busy === 'formal-upload'}
          >
            {busy === 'formal-upload' ? '识别中…' : '识别并匹配库存'}
          </button>
        </div>

        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>导入号</th>
                <th>船厂出货单号</th>
                <th>来源文件</th>
                <th>行数 / 数量</th>
                <th>状态</th>
                <th>出库单</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {formalManifests.map((item) => (
                <tr key={item.manifest_id}>
                  <td>{item.manifest_no}</td>
                  <td>{item.factory_shipment_no}</td>
                  <td>{item.source_file_name}</td>
                  <td>{item.line_count} / {item.total_quantity}</td>
                  <td>
                    <Status
                      value={item.outbound_order_status || item.status}
                      labels={FORMAL_STATUS}
                    />
                  </td>
                  <td>{item.outbound_order_no || '—'}</td>
                  <td className="shipyard-actions">
                    <button
                      className="btn btn-sm"
                      onClick={() => openFormalManifest(item.manifest_id)}
                      disabled={busy === `formal-${item.manifest_id}`}
                    >
                      查看
                    </button>
                    {item.outbound_order_id && (
                      <button
                        className="btn btn-sm"
                        onClick={() => openFormalOrder(item.outbound_order_id)}
                      >
                        出库任务
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {!formalManifests.length && (
                <tr>
                  <td colSpan="7" className="empty-state">
                    还没有正式出库清单
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {selectedFormal && (
        <section className="settings-section" ref={formalPreviewRef}>
          <div className="shipyard-section-heading">
            <div>
              <h3>正式出库清单预览 · {selectedFormal.manifest.manifest_no}</h3>
              <p className="settings-note">
                船厂出货单号 {selectedFormal.manifest.factory_shipment_no}
                {' · '}
                可匹配 {selectedFormal.validation.summary.task_count} 张整票任务
                {' · '}
                缺口 {selectedFormal.validation.summary.total_shortage_quantity}
              </p>
            </div>
            <div className="shipyard-actions">
              {selectedFormal.manifest.status === 'PARSED' && (
                <button
                  className="btn btn-primary"
                  onClick={confirmFormalManifest}
                  disabled={
                    busy === 'formal-confirm'
                    || !!selectedFormal.validation.blocking_errors?.length
                  }
                >
                  {busy === 'formal-confirm' ? '生成中…' : '确认并生成正式出库单'}
                </button>
              )}
              <a
                className="btn"
                href={`/api/shipyard/outbound/formal-manifests/${selectedFormal.manifest.manifest_id}/file`}
              >
                下载原文件
              </a>
            </div>
          </div>

          {!!selectedFormal.validation.blocking_errors?.length && (
            <div className="shipyard-warning-list">
              {selectedFormal.validation.blocking_errors.map((error) => (
                <div key={error.code}>
                  <strong>{error.code}</strong> {error.message}
                </div>
              ))}
              <a className="btn btn-sm" href="/shipyard-settings">
                去处理船号与区域绑定
              </a>
            </div>
          )}

          <div className="table-wrap shipyard-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>清单行</th>
                  <th>物料编码 / 品名</th>
                  <th>船号 / 制作图</th>
                  <th>需求</th>
                  <th>已分配</th>
                  <th>缺口</th>
                  <th>匹配票货</th>
                  <th>区域 / 托盘后缀</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {selectedFormal.lines.map((line) => (
                  <tr key={line.line_id}>
                    <td>{line.source_row_no}</td>
                    <td>
                      <strong>{line.material_code}</strong>
                      <div>{line.item_name}</div>
                      <div className="settings-note">{line.item_spec || '—'}</div>
                    </td>
                    <td>
                      {line.ship_no}
                      <div className="settings-note">{line.production_drawing_no}</div>
                    </td>
                    <td>{line.demand_quantity} {line.unit}</td>
                    <td>{line.allocated_quantity}</td>
                    <td className={Number(line.shortage_quantity) > 0 ? 'shipyard-error-text' : ''}>
                      {line.shortage_quantity}
                    </td>
                    <td>
                      {line.tasks.length
                        ? line.tasks.map((task) => (
                          <div key={task.ticket_id}>
                            {task.ticket_no} · {task.available_quantity}
                          </div>
                        ))
                        : '—'}
                    </td>
                    <td>
                      {line.tasks.length
                        ? line.tasks.map((task) => (
                          <div key={task.ticket_id}>
                            {task.location_path_snapshot}
                            <div className="settings-note">
                              {task.tag_suffixes.join('、')}
                            </div>
                          </div>
                        ))
                        : '—'}
                    </td>
                    <td><Status value={line.match_status} labels={FORMAL_STATUS} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>正式出库任务与装车</h3>
            <p className="settings-note">
              每张任务对应一张不可拆的整票托组；现场小程序扫齐全部吊牌后才扣库，
              全部出库且无缺口后才能装车。
            </p>
          </div>
        </div>
        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>出库单号</th>
                <th>船厂出货单号</th>
                <th>行数</th>
                <th>整票任务</th>
                <th>待出库</th>
                <th>应出 / 已出</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {formalOrders.map((item) => (
                <tr key={item.outbound_order_id}>
                  <td>{item.outbound_order_no}</td>
                  <td>{item.factory_shipment_no}</td>
                  <td>{item.line_count}</td>
                  <td>{item.task_count}</td>
                  <td>{item.pending_task_count}</td>
                  <td>
                    {item.total_required_quantity} / {item.total_fulfilled_quantity}
                  </td>
                  <td><Status value={item.status} labels={FORMAL_STATUS} /></td>
                  <td>
                    <button
                      className="btn btn-sm"
                      onClick={() => openFormalOrder(item.outbound_order_id)}
                      disabled={busy === `formal-order-${item.outbound_order_id}`}
                    >
                      查看任务
                    </button>
                  </td>
                </tr>
              ))}
              {!formalOrders.length && (
                <tr>
                  <td colSpan="8" className="empty-state">
                    还没有正式出库单
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {selectedFormalOrder && (
        <section className="settings-section" ref={formalOrderRef}>
          <div className="shipyard-section-heading">
            <div>
              <h3>正式出库单 · {selectedFormalOrder.order.outbound_order_no}</h3>
              <p className="settings-note">
                {selectedFormalOrder.order.factory_shipment_no}
                {' · '}
                待出库 {selectedFormalOrder.summary.pending_task_count} 票
                {' · '}
                已出库 {selectedFormalOrder.summary.outbound_task_count} 票
                {' · '}
                <Status
                  value={selectedFormalOrder.order.status}
                  labels={FORMAL_STATUS}
                />
              </p>
            </div>
          </div>
          <div className="table-wrap shipyard-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>清单行</th>
                  <th>物料</th>
                  <th>票货 / 区域</th>
                  <th>整票数量</th>
                  <th>托盘后缀</th>
                  <th>出库人 / 时间</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {selectedFormalOrder.tasks.map((task) => (
                  <tr key={task.task_id}>
                    <td>{task.source_row_no}</td>
                    <td>
                      <strong>{task.material_code}</strong>
                      <div>{task.item_name}</div>
                      <div className="settings-note">{task.item_spec || '—'}</div>
                    </td>
                    <td>
                      {task.ticket_no}
                      <div className="settings-note">{task.location_path_snapshot}</div>
                    </td>
                    <td>{task.planned_quantity}</td>
                    <td>{task.expected_tag_suffixes.join('、')}</td>
                    <td>
                      {task.confirmation
                        ? `${task.confirmation.outbound_by} · ${formatTime(task.confirmation.outbound_at)}`
                        : '—'}
                    </td>
                    <td><Status value={task.status} labels={FORMAL_STATUS} /></td>
                  </tr>
                ))}
                {!selectedFormalOrder.tasks.length && (
                  <tr>
                    <td colSpan="7" className="empty-state">
                      该出库单没有可执行整票任务，请处理库存缺口
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

        </>
      ) : (
        <>
      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>导入配送需求</h3>
            <p className="settings-note">
              拖入船厂托盘工单。系统先识别、保存原文件，人工确认后才参与派工匹配。
            </p>
          </div>
        </div>
        <div
          className={`shipyard-dropzone${dragging ? ' is-dragging' : ''}`}
          onDragEnter={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            event.preventDefault();
            setDragging(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            acceptFile(event.dataTransfer.files?.[0]);
          }}
        >
          <input
            ref={fileRef}
            id="shipyard-demand-file"
            type="file"
            accept=".xlsx"
            onChange={(event) => acceptFile(event.target.files?.[0])}
          />
          <label htmlFor="shipyard-demand-file">
            <strong>{selectedFile ? selectedFile.name : '拖入或选择船厂托盘工单'}</strong>
            <span>仅支持 .xlsx；单文件最大 10 MiB</span>
          </label>
          <button
            className="btn btn-primary"
            onClick={uploadDemand}
            disabled={!selectedFile || busy === 'upload'}
          >
            {busy === 'upload' ? '识别中…' : '识别并保存'}
          </button>
        </div>

        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>导入号</th>
                <th>来源文件</th>
                <th>票数</th>
                <th>状态</th>
                <th>导入时间</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {imports.map((item) => (
                <tr key={item.import_id}>
                  <td>{item.import_no}</td>
                  <td>{item.source_file_name}</td>
                  <td>{item.line_count}</td>
                  <td><Status value={item.status} labels={IMPORT_STATUS} /></td>
                  <td>{formatTime(item.created_at)}</td>
                  <td>
                    <button
                      className="btn btn-sm"
                      onClick={() => openImport(item.import_id)}
                      disabled={busy === `import-${item.import_id}`}
                    >
                      查看
                    </button>
                  </td>
                </tr>
              ))}
              {!imports.length && (
                <tr><td colSpan="6" className="empty-state">还没有配送需求导入</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {selectedImport && (
        <section className="settings-section" ref={importPreviewRef}>
          <div className="shipyard-section-heading">
            <div>
              <h3>配送需求预览 · {selectedImport.import.import_no}</h3>
              <p className="settings-note">
                {selectedImport.import.metadata?.sheet_name || '—'}
                {' · '}
                {selectedImport.lines.length} 票
              </p>
            </div>
            <div className="shipyard-actions">
              {selectedImport.import.status === 'PARSED' && (
                <button
                  className="btn btn-primary"
                  onClick={confirmImport}
                  disabled={busy === 'confirm-import'}
                >
                  确认导入
                </button>
              )}
              {selectedImport.import.status !== 'VOID' && (
                <>
                  <input
                    className="input shipyard-inline-input"
                    value={voidReason}
                    onChange={(event) => setVoidReason(event.target.value)}
                    placeholder="作废原因"
                  />
                  <button
                    className="btn"
                    onClick={voidImport}
                    disabled={busy === 'void-import'}
                  >
                    作废
                  </button>
                </>
              )}
              <a
                className="btn"
                href={`/api/shipyard/outbound/distribution-imports/${selectedImport.import.import_id}/file`}
              >
                下载原文件
              </a>
            </div>
          </div>

          {!!selectedImport.warnings?.length && (
            <div className="shipyard-warning-list">
              {selectedImport.warnings.map((warning) => (
                <div key={`${warning.code}-${warning.column || ''}`}>
                  <strong>{warning.code}</strong> {warning.message}
                </div>
              ))}
            </div>
          )}

          <div className="table-wrap shipyard-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>来源行</th>
                  <th>工程号</th>
                  <th>分段</th>
                  <th>托盘号</th>
                  <th>托盘名称</th>
                  <th>需求时间</th>
                  <th>配送状态</th>
                </tr>
              </thead>
              <tbody>
                {selectedImport.lines.map((line) => (
                  <tr key={line.line_id}>
                    <td>{line.source_row_no}</td>
                    <td>{line.engineering_no}</td>
                    <td>{line.section_no}</td>
                    <td>{line.pallet_no}</td>
                    <td>{line.pallet_name}</td>
                    <td>{line.demand_window_raw || '—'}</td>
                    <td>{line.delivery_status || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>派工单与需求匹配</h3>
            <p className="settings-note">
              唯一匹配自动通过；歧义或未匹配保留在派工单中，不阻塞现场先收单。
            </p>
          </div>
        </div>
        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>派工单号</th>
                <th>派工日期</th>
                <th>明细</th>
                <th>已匹配</th>
                <th>待选</th>
                <th>未匹配</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {dispatches.map((item) => (
                <tr key={item.dispatch_order_id}>
                  <td>{item.dispatch_no}</td>
                  <td>{item.dispatch_date}</td>
                  <td>{item.line_count}</td>
                  <td>{item.matched_line_count}</td>
                  <td>{item.ambiguous_line_count}</td>
                  <td>{item.unmatched_line_count}</td>
                  <td><Status value={item.status} labels={DISPATCH_STATUS} /></td>
                  <td>
                    <button
                      className="btn btn-sm"
                      onClick={() => openDispatch(item.dispatch_order_id)}
                      disabled={busy === `dispatch-${item.dispatch_order_id}`}
                    >
                      处理
                    </button>
                  </td>
                </tr>
              ))}
              {!dispatches.length && (
                <tr><td colSpan="8" className="empty-state">还没有派工单</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {selectedDispatch && (
        <section className="settings-section" ref={dispatchPreviewRef}>
          <div className="shipyard-section-heading">
            <div>
              <h3>派工单 · {selectedDispatch.dispatch.dispatch_no}</h3>
              <p className="settings-note">
                {selectedDispatch.dispatch.dispatch_date}
                {' · '}
                来源 {selectedDispatch.dispatch.source_file_name || selectedDispatch.dispatch.source_type}
              </p>
            </div>
          </div>
          <div className="table-wrap shipyard-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>行</th>
                  <th>工程号</th>
                  <th>托盘号</th>
                  <th>物资</th>
                  <th>数量</th>
                  <th>匹配状态</th>
                  <th>来源需求</th>
                  <th>人工处理</th>
                </tr>
              </thead>
              <tbody>
                {selectedDispatch.lines.map((line) => {
                  const candidates = line.match_details?.candidates || [];
                  return (
                    <tr key={line.dispatch_line_id}>
                      <td>{line.line_no}</td>
                      <td>{line.engineering_no}</td>
                      <td>{line.pallet_no}</td>
                      <td>
                        {line.material_spec_raw}
                        {line.specification && (
                          <div className="settings-note">{line.specification}</div>
                        )}
                      </td>
                      <td>{line.quantity} {line.unit}</td>
                      <td><Status value={line.match_status} labels={MATCH_STATUS} /></td>
                      <td>
                        {line.demand_import_no
                          ? `${line.demand_import_no} / 行 ${line.demand_source_row_no || '—'}`
                          : '—'}
                      </td>
                      <td>
                        {['AMBIGUOUS', 'UNMATCHED'].includes(line.match_status) && (
                          <div className="shipyard-match-control">
                            {candidates.length > 0 ? (
                              <select
                                className="input"
                                aria-label={`派工行 ${line.line_no} 候选需求`}
                                value={manualDemandIds[line.dispatch_line_id] || ''}
                                onChange={(event) => setManualDemandIds((current) => ({
                                  ...current,
                                  [line.dispatch_line_id]: event.target.value,
                                }))}
                              >
                                <option value="">选择候选</option>
                                {candidates.map((candidate) => (
                                  <option key={candidate.line_id} value={candidate.line_id}>
                                    {candidate.import_no} / 行 {candidate.source_row_no} / {candidate.pallet_name}
                                  </option>
                                ))}
                              </select>
                            ) : (
                              <input
                                className="input"
                                aria-label={`派工行 ${line.line_no} 需求行 ID`}
                                placeholder="需求行 ID"
                                value={manualDemandIds[line.dispatch_line_id] || ''}
                                onChange={(event) => setManualDemandIds((current) => ({
                                  ...current,
                                  [line.dispatch_line_id]: event.target.value,
                                }))}
                              />
                            )}
                            <button
                              className="btn btn-sm"
                              onClick={() => matchLine(line)}
                              disabled={
                                !manualDemandIds[line.dispatch_line_id]
                                || busy === `match-${line.dispatch_line_id}`
                              }
                            >
                              匹配
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="shipyard-note-builder">
            <div>
              <h4>生成前置仓发货单</h4>
              <p className="settings-note">
                一张派工单只生成一张发货单，系统会固化来源需求行和打印内容。
              </p>
            </div>
            {notesByDispatch.has(selectedDispatch.dispatch.dispatch_order_id) ? (
              <button
                className="btn btn-primary"
                onClick={() => openDeliveryNote(
                  notesByDispatch.get(selectedDispatch.dispatch.dispatch_order_id).delivery_note_id,
                )}
              >
                打开已生成发货单
              </button>
            ) : (
              <div className="shipyard-actions">
                <label>
                  <span>发货单号</span>
                  <input
                    className="input"
                    value={noteNo}
                    onChange={(event) => setNoteNo(event.target.value)}
                  />
                </label>
                <label>
                  <span>发放日期</span>
                  <input
                    className="input"
                    type="date"
                    value={issueDate}
                    onChange={(event) => setIssueDate(event.target.value)}
                  />
                </label>
                <button
                  className="btn btn-primary"
                  onClick={createDeliveryNote}
                  disabled={busy === 'create-note'}
                >
                  {busy === 'create-note' ? '生成中…' : '生成发货单'}
                </button>
              </div>
            )}
          </div>
        </section>
      )}

      <section className="settings-section">
        <div className="shipyard-section-heading">
          <div>
            <h3>已生成发货单</h3>
            <p className="settings-note">打开详情核对来源，再进入 A4 横向打印视图。</p>
          </div>
        </div>
        <div className="table-wrap shipyard-table-wrap">
          <table>
            <thead>
              <tr>
                <th>发货单号</th>
                <th>来源派工单</th>
                <th>发放日期</th>
                <th>行数</th>
                <th>总数量</th>
                <th>状态</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {deliveryNotes.map((item) => (
                <tr key={item.delivery_note_id}>
                  <td>{item.delivery_note_no}</td>
                  <td>{item.dispatch_no}</td>
                  <td>{item.issue_date}</td>
                  <td>{item.line_count}</td>
                  <td>{item.total_quantity}</td>
                  <td>{item.status}</td>
                  <td className="shipyard-actions">
                    <button
                      className="btn btn-sm"
                      onClick={() => openDeliveryNote(item.delivery_note_id)}
                      disabled={busy === `note-${item.delivery_note_id}`}
                    >
                      详情
                    </button>
                    <a
                      className="btn btn-sm"
                      href={`/api/shipyard/outbound/delivery-notes/${item.delivery_note_id}/print`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      打印
                    </a>
                  </td>
                </tr>
              ))}
              {!deliveryNotes.length && (
                <tr><td colSpan="7" className="empty-state">还没有发货单</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {selectedNote && (
        <section className="settings-section" ref={deliveryNoteRef}>
          <div className="shipyard-section-heading">
            <div>
              <h3>发货单 · {selectedNote.delivery_note.delivery_note_no}</h3>
              <p className="settings-note">
                来源 {selectedNote.delivery_note.dispatch_no}
                {' · '}
                {selectedNote.delivery_note.issue_date}
              </p>
            </div>
            <a
              className="btn btn-primary"
              href={`/api/shipyard/outbound/delivery-notes/${selectedNote.delivery_note.delivery_note_id}/print`}
              target="_blank"
              rel="noreferrer"
            >
              打印 A4 横向
            </a>
          </div>
          <div className="table-wrap shipyard-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>行</th>
                  <th>工程号</th>
                  <th>分段</th>
                  <th>托盘号</th>
                  <th>物资规格</th>
                  <th>数量</th>
                  <th>来源需求</th>
                </tr>
              </thead>
              <tbody>
                {selectedNote.lines.map((line) => (
                  <tr key={line.delivery_line_id}>
                    <td>{line.line_no}</td>
                    <td>{line.engineering_no}</td>
                    <td>{line.section_no || '—'}</td>
                    <td>{line.pallet_no}</td>
                    <td>
                      {line.material_description}
                      {line.specification && (
                        <div className="settings-note">{line.specification}</div>
                      )}
                    </td>
                    <td>{line.quantity} {line.unit}</td>
                    <td>
                      {line.demand_import_no
                        ? `${line.demand_import_no} / 行 ${line.demand_source_row_no || '—'}`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
        </>
      )}
    </div>
  );
}
