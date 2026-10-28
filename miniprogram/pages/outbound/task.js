const { createIdempotencyKey } = require('../../utils/idempotency');
const { buildOutboundConfirmation } = require('../../utils/payload');
const { request, upload } = require('../../utils/request');
const {
  chooseNextSuffix,
  normalizeSuffix,
} = require('../../utils/scan');
const { requireSession } = require('../../utils/session');

Page({
  data: {
    session: null,
    taskId: '',
    orderId: '',
    task: null,
    order: null,
    scans: [],
    nextSuffix: '',
    busy: false,
    complete: false,
    can_confirm: false,
    suffixPickerVisible: false,
    suffixChoices: [],
    pendingCapture: null,
  },

  onLoad(options) {
    const session = requireSession();
    if (!session) return;
    this.setData({
      session,
      taskId: options.taskId,
      orderId: options.orderId,
    });
    this.loadTask();
  },

  async loadTask() {
    this.setData({ busy: true });
    try {
      const data = await request({
        path: `/shipyard/outbound/mobile/orders/${this.data.orderId}`,
      });
      const task = (data.tasks || []).find(
        (item) => item.task_id === this.data.taskId,
      );
      if (!task) throw new Error('出库任务不存在');
      const nextSuffix = chooseNextSuffix(
        task.expected_tag_suffixes,
        this.data.scans.map((item) => item.suffix),
      );
      const decoratedTask = {
        ...task,
        suffix_text: (task.expected_tag_suffixes || []).join('、'),
      };
      this.setData({
        task: decoratedTask,
        order: data,
        nextSuffix,
        complete: (data.tasks || []).every((item) => item.status === 'OUTBOUND'),
        scans: task.status === 'OUTBOUND' ? [] : this.data.scans,
      });
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },

  async captureLabel() {
    const { task, busy } = this.data;
    if (!task || busy) return;
    if (task.status !== 'PENDING') {
      wx.showToast({ title: '该票已经完成出库', icon: 'none' });
      return;
    }
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
        path: `/shipyard/outbound/tasks/${task.task_id}/label-recognition`,
        filePath,
        timeout: 45000,
      });
      const mismatches = result.mismatches || [];
      const blocking = mismatches.filter((item) => item.field !== 'tag_suffix');
      if (blocking.length) {
        this.showLabelError(result, blocking);
        return;
      }
      const suffix = normalizeSuffix(result.recognized?.tag_suffix);
      if (result.needs_suffix || !suffix) {
        this.openSuffixPicker(result);
        return;
      } else {
        const suffixMismatch = mismatches.find((item) => item.field === 'tag_suffix');
        if (suffixMismatch) {
          this.showLabelError(result, [suffixMismatch]);
          return;
        }
      }
      this.addCapture(result, suffix);
    } catch (error) {
      if (error.errMsg && error.errMsg.includes('cancel')) return;
      this.showLabelError(error.response?.data || {}, [], error.message);
    } finally {
      this.setData({ busy: false });
    }
  },

  openSuffixPicker(result) {
    const completed = new Set(this.data.scans.map((item) => item.suffix));
    const suffixChoices = (this.data.task.expected_tag_suffixes || [])
      .filter((suffix) => !completed.has(suffix));
    if (!suffixChoices.length) {
      this.showLabelError(result, [{
        field: 'pallet_count',
        expected: this.data.task.expected_pallet_count,
        actual: this.data.scans.length,
      }]);
      return;
    }
    this.setData({
      suffixPickerVisible: true,
      suffixChoices,
      pendingCapture: result,
    });
  },

  selectSuffix(event) {
    const suffix = normalizeSuffix(event.currentTarget.dataset.suffix);
    const pendingCapture = this.data.pendingCapture;
    if (!suffix || !pendingCapture) return;
    if (!(this.data.task.expected_tag_suffixes || []).includes(suffix)) {
      wx.showToast({ title: '该托盘不属于当前整票', icon: 'none' });
      return;
    }
    if (this.data.scans.some((item) => item.suffix === suffix)) {
      wx.showToast({ title: `托盘 ${suffix} 已经拍过`, icon: 'none' });
      return;
    }
    this.setData({
      suffixPickerVisible: false,
      suffixChoices: [],
      pendingCapture: null,
    });
    this.addCapture(pendingCapture, suffix);
  },

  cancelSuffixPicker() {
    this.setData({
      suffixPickerVisible: false,
      suffixChoices: [],
      pendingCapture: null,
    });
    wx.showToast({ title: '照片已留证，本次未计入核销', icon: 'none' });
  },

  addCapture(result, suffix) {
    const task = this.data.task;
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
    const nextSuffix = chooseNextSuffix(
      task.expected_tag_suffixes,
      scans.map((item) => item.suffix),
    );
    this.setData({
      scans,
      nextSuffix,
      can_confirm: scans.length === task.expected_tag_suffixes.length,
    });
    wx.showToast({ title: `托盘 ${suffix} 识别通过`, icon: 'success' });
  },

  showLabelError(result, mismatches = [], fallback = '') {
    const candidates = result.candidate_tasks || [];
    if (candidates.length) {
      const goToCandidate = (candidate) => {
        wx.redirectTo({
          url: `/pages/outbound/task?taskId=${candidate.task_id}&orderId=${candidate.outbound_order_id}`,
        });
      };
      if (candidates.length === 1) {
        wx.showModal({
          title: '吊牌属于另一票货物',
          content: `照片内容对应任务 ${candidates[0].task_no}。当前任务不能继续核销。`,
          confirmText: '去正确任务',
          cancelText: '留在当前票',
          success: (choice) => {
            if (choice.confirm) goToCandidate(candidates[0]);
          },
        });
        return;
      }
      wx.showActionSheet({
        itemList: candidates.map(
          (item) => `${item.outbound_order_no} · ${item.task_no}`,
        ),
        success: (choice) => {
          const candidate = candidates[choice.tapIndex];
          if (candidate) goToCandidate(candidate);
        },
      });
      return;
    }
    const labels = {
      ship_no: '船号',
      engineering_no: '工程号',
      production_drawing_no: '制作图号',
      material_code: '物料编码',
      item_name: '品名',
      label_quantity: '件数',
      tag_suffix: '托盘后缀',
    };
    const details = mismatches.slice(0, 4).map((item) => {
      const label = labels[item.field] || item.field;
      if (item.reason === 'REQUIRED') return `${label}未识别`;
      return `${label}不一致，应为 ${item.expected || '—'}，识别为 ${item.actual || '—'}`;
    }).join('\n');
    if (result.mismatches?.length && !mismatches.length) {
      return this.showLabelError(result, result.mismatches, fallback);
    }
    if (result.matched_task_id && result.matched_outbound_order_id) {
      wx.showModal({
        title: '吊牌属于另一票货物',
        content: `该吊牌对应任务 ${result.matched_task_no || result.matched_task_id}。当前任务不能继续核销。`,
        confirmText: '去正确任务',
        cancelText: '留在当前票',
        success: (choice) => {
          if (!choice.confirm) return;
          wx.redirectTo({
            url: `/pages/outbound/task?taskId=${result.matched_task_id}&orderId=${result.matched_outbound_order_id}`,
          });
        },
      });
      return;
    }
    wx.showModal({
      title: '吊牌未通过',
      content: `${details || fallback || '请调整角度后重拍'}\n\n照片已留证，本次未计入核销。`,
      showCancel: false,
      confirmText: '重拍',
    });
  },

  removeScan(event) {
    const suffix = event.currentTarget.dataset.suffix;
    const scans = this.data.scans.filter((item) => item.suffix !== suffix);
    this.setData({
      scans,
      nextSuffix: chooseNextSuffix(
        this.data.task.expected_tag_suffixes,
        scans.map((item) => item.suffix),
      ),
      can_confirm: scans.length === this.data.task.expected_tag_suffixes.length,
    });
  },

  async confirmOutbound() {
    const { task, scans } = this.data;
    if (!task) return;
    const expected = task.expected_tag_suffixes || [];
    if (scans.length !== expected.length) {
      wx.showToast({
        title: `整票未齐套，还缺 ${expected.length - scans.length} 托`,
        icon: 'none',
      });
      return;
    }
    const body = buildOutboundConfirmation(
      createIdempotencyKey('outbound'),
      scans,
      `wx-${this.data.session.user.username}`,
    );
    this.setData({ busy: true });
    try {
      const result = await request({
        path: `/shipyard/outbound/tasks/${task.task_id}/confirm`,
        method: 'POST',
        data: body,
      });
      wx.showModal({
        title: '整票出库完成',
        content: `扣减 ${result.confirmation.quantity} 件\n区域 ${result.confirmation.location_path_snapshot}\n出库单状态 ${result.order.status}`,
        showCancel: false,
        success: () => wx.navigateBack(),
      });
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },
});
