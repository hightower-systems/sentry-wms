const { createIdempotencyKey } = require('../../utils/idempotency');
const { buildReceiptConfirmation } = require('../../utils/payload');
const {
  remainingQuantity,
  scanProgress,
  validateReceiptDraft,
} = require('../../utils/receipt');
const { request, upload } = require('../../utils/request');
const { normalizeSuffix } = require('../../utils/scan');
const { requireSession } = require('../../utils/session');

const STATUS_LABELS = {
  PARSED: '待确认',
  CONFIRMED: '待收货',
  PARTIAL: '部分到货',
  CLOSED: '已到齐',
  PENDING: '待收货',
  RECEIVED: '已到货',
  EXPECTED: '待收货',
  IN_STOCK: '已入库',
  OUTBOUND: '已出库',
  EXCEPTION: '异常',
};

function statusClass(status) {
  if (['CLOSED', 'RECEIVED', 'IN_STOCK'].includes(status)) return 'chip-success';
  if (['PARTIAL', 'EXCEPTION'].includes(status)) return 'chip-warning';
  return '';
}

function numberText(value) {
  const number = Number(value || 0);
  return Number.isInteger(number) ? String(number) : number.toFixed(3);
}

Page({
  data: {
    session: null,
    warehouseId: 1,
    manifests: [],
    selectedManifest: null,
    selectedManifestId: '',
    selectedTicket: null,
    scans: [],
    quantity: '',
    differenceReason: '',
    busy: false,
    loading: false,
    cameraVisible: false,
    pendingSuffix: '',
    nextSuffix: '',
    palletProgressText: '',
    remainingQuantityText: '',
  },

  onLoad() {
    const session = requireSession();
    if (!session) return;
    this.setData({
      session,
      warehouseId: session.warehouseId,
    });
    this.loadManifests();
  },

  onPullDownRefresh() {
    this.loadManifests().finally(() => wx.stopPullDownRefresh());
  },

  async loadManifests() {
    this.setData({ loading: true });
    try {
      const data = await request({
        path: `/shipyard/inbound/manifests?warehouse_id=${this.data.warehouseId}`,
      });
      const manifests = (data.manifests || []).map((item) => ({
        ...item,
        status_label: STATUS_LABELS[item.status] || item.status,
        status_class: statusClass(item.status),
      }));
      this.setData({ manifests });
      if (this.data.selectedManifest) {
        await this.openManifest(this.data.selectedManifest.manifest.manifest_id);
      } else {
        const receivable = manifests.filter((item) => (
          ['CONFIRMED', 'PARTIAL'].includes(item.status)
          || Number(item.pending_ticket_count || 0) > 0
        ));
        if (receivable.length === 1) {
          await this.openManifest(receivable[0].manifest_id);
        }
      }
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  async openManifest(manifestId) {
    this.setData({ busy: true });
    try {
      const data = await request({
        path: `/shipyard/inbound/manifests/${manifestId}`,
      });
      const tickets = (data.tickets || []).map((ticket) => ({
        ...ticket,
        status_label: STATUS_LABELS[ticket.status] || ticket.status,
        status_class: statusClass(ticket.status),
        short_quantity_text: numberText(ticket.short_quantity),
        remaining_quantity_text: numberText(remainingQuantity(ticket)),
        pallet_progress_text: scanProgress(ticket, []).text,
        can_receive: ['EXPECTED', 'PARTIAL'].includes(ticket.status),
      }));
      const selectedManifest = { ...data, tickets };
      this.setData({
        selectedManifest,
        selectedManifestId: manifestId,
        selectedTicket: null,
        scans: [],
      });
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  onManifestTap(event) {
    this.openManifest(event.currentTarget.dataset.id);
  },

  startTicket(event) {
    const ticketId = event.currentTarget.dataset.id;
    const ticket = this.data.selectedManifest.tickets.find(
      (item) => item.ticket_id === ticketId,
    );
    if (!ticket) return;
    if (!['EXPECTED', 'PARTIAL'].includes(ticket.status)) {
      wx.showToast({ title: '该票当前不能收货', icon: 'none' });
      return;
    }
    const remaining = Math.max(
      remainingQuantity(ticket),
      0,
    );
    const progress = scanProgress(ticket, []);
    this.setData({
      selectedTicket: ticket,
      scans: [],
      quantity: numberText(remaining),
      differenceReason: '',
      palletProgressText: progress.text,
      remainingQuantityText: numberText(remaining),
    });
    wx.pageScrollTo({ selector: '#ticket-workbench', duration: 250 });
  },

  async captureLabel() {
    const ticket = this.data.selectedTicket;
    if (!ticket || this.data.busy) return;
    this.setData({ busy: true });
    try {
      const media = await wx.chooseMedia({
        count: 1,
        mediaType: ['image'],
        sourceType: ['camera'],
        sizeType: ['compressed'],
      });
      const filePath = media.tempFiles[0].tempFilePath;
      const result = await upload({
        path: `/shipyard/inbound/tickets/${ticket.ticket_id}/label-recognition`,
        filePath,
        timeout: 45000,
      });
      const mismatches = result.mismatches || [];
      const blocking = mismatches.filter((item) => item.field !== 'tag_suffix');
      if (blocking.length) {
        this.showLabelError(blocking);
        return;
      }
      let suffix = normalizeSuffix(result.recognized?.tag_suffix);
      if (result.needs_suffix || !suffix) {
        suffix = await this.askSuffix();
      } else {
        const suffixMismatch = mismatches.find((item) => item.field === 'tag_suffix');
        if (suffixMismatch) {
          this.showLabelError([suffixMismatch]);
          return;
        }
      }
      if (!suffix) return;
      if (this.data.scans.some((item) => item.suffix === suffix)) {
        wx.showToast({ title: `托盘 ${suffix} 已拍摄，请检查是否拿错`, icon: 'none' });
        return;
      }
      const scans = [
        ...this.data.scans,
        {
          suffix,
          recognition: result.recognition,
          recognized: result.recognized || {},
          photo: result.photo,
          validation: result,
        },
      ];
      const progress = scanProgress(ticket, scans);
      const nextSuffix = progress.remaining === null
        ? '可继续拍摄下一托'
        : progress.remaining > 0
          ? `还差 ${progress.remaining} 托`
        : '';
      this.setData({
        scans,
        nextSuffix,
        palletProgressText: progress.text,
      });
      wx.showToast({ title: `托盘 ${suffix} 已识别`, icon: 'success' });
    } catch (error) {
      if (error.errMsg && error.errMsg.includes('cancel')) return;
      this.showLabelError(
        error.response?.data?.mismatches || [],
        error.message || '吊牌识别失败，请重拍',
      );
    } finally {
      this.setData({ busy: false });
    }
  },

  showLabelError(mismatches, fallback = '吊牌字段与当前票货不一致') {
    const labels = {
      ship_no: '船号',
      engineering_no: '工程号',
      production_drawing_no: '制作图号',
      material_code: '物料编码',
      item_name: '品名',
      label_quantity: '件数',
      tag_suffix: '托盘后缀',
    };
    const content = mismatches.length
      ? mismatches.slice(0, 4).map((item) => {
        const label = labels[item.field] || item.field;
        if (item.reason === 'REQUIRED') return `${label}未识别，请重拍`;
        if (item.reason === 'DUPLICATE') return `${label} ${item.actual || ''} 已使用`;
        if (item.reason === 'TOO_MANY') {
          return `本票托盘数量已满，预计 ${item.expected} 托`;
        }
        return `${label}不一致，应为 ${item.expected || '—'}，识别为 ${item.actual || '—'}`;
      }).join('\n')
      : fallback;
    wx.showModal({
      title: '吊牌未通过',
      content: `${content}\n\n照片已留证，本次未计入收货。`,
      showCancel: false,
      confirmText: '重拍',
    });
  },

  askSuffix() {
    return new Promise((resolve) => {
      wx.showModal({
        title: '补录托盘后缀',
        content: 'OCR 未读到后缀，请按吊牌核对后输入。',
        editable: true,
        placeholderText: '例如 1-1',
        success(result) {
          if (!result.confirm) {
            resolve('');
            return;
          }
          const suffix = normalizeSuffix(result.content);
          if (!suffix) {
            wx.showToast({ title: '后缀格式应为 1-1', icon: 'none' });
          }
          resolve(suffix);
        },
        fail() {
          resolve('');
        },
      });
    });
  },

  onQuantityInput(event) {
    this.setData({ quantity: event.detail.value });
  },

  onDifferenceReasonInput(event) {
    this.setData({ differenceReason: event.detail.value });
  },

  removeScan(event) {
    const suffix = event.currentTarget.dataset.suffix;
    const scans = this.data.scans.filter((item) => item.suffix !== suffix);
    const progress = scanProgress(this.data.selectedTicket, scans);
    this.setData({
      scans,
      palletProgressText: progress.text,
      nextSuffix: progress.remaining === null
        ? '可继续拍摄下一托'
        : progress.remaining > 0
          ? `还差 ${progress.remaining} 托`
          : '',
    });
  },

  async confirmReceipt() {
    const { selectedTicket, scans, quantity } = this.data;
    const differenceReason = this.data.differenceReason.trim();
    const draft = validateReceiptDraft({
      ticket: selectedTicket,
      scans,
      quantity,
      differenceReason,
    });
    if (!draft.ok) {
      wx.showModal({
        title: '还不能提交收货',
        content: draft.error,
        showCancel: false,
      });
      return;
    }
    const body = buildReceiptConfirmation({
      idempotencyKey: createIdempotencyKey('receipt'),
      quantity,
      evidence: scans,
      differenceReason,
      deviceId: `wx-${this.data.session.user.username}`,
    });
    this.setData({ busy: true });
    try {
      const result = await request({
        path: `/shipyard/inbound/tickets/${selectedTicket.ticket_id}/receipts`,
        method: 'POST',
        data: body,
      });
      wx.showModal({
        title: '收货已完成',
        content: `${result.ticket.item_name}\n实收 ${result.ticket.received_quantity} 件\n区域 ${result.receipt.location_path_snapshot}`,
        showCancel: false,
        success: () => {
          this.setData({
            selectedTicket: null,
            scans: [],
            nextSuffix: '',
            palletProgressText: '',
            remainingQuantityText: '',
          });
          this.loadManifests();
        },
      });
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },
});
