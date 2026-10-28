const { request } = require('../../utils/request');
const { clearSession, requireSession, saveSession } = require('../../utils/session');

Page({
  data: {
    session: null,
    warehouseId: '',
    warehouses: [],
    warehouseName: '',
  },

  onShow() {
    const session = requireSession();
    if (!session) return;
    this.setData({
      session,
      warehouseId: String(session.warehouseId || ''),
    });
    this.loadWarehouses(session);
  },

  async loadWarehouses(session) {
    try {
      const data = await request({ path: '/warehouses/list' });
      const rawWarehouses = Array.isArray(data)
        ? data
        : (data.warehouses || []);
      const warehouses = rawWarehouses.map((item) => ({
        warehouse_id: item.warehouse_id || item.id,
        warehouse_name: item.warehouse_name || item.name,
        warehouse_code: item.warehouse_code || item.code,
      }));
      const allowed = new Set(
        session.user.warehouse_ids
          || (session.warehouseId ? [session.warehouseId] : []),
      );
      const visible = session.user.role === 'ADMIN'
        ? warehouses
        : warehouses.filter((item) => allowed.has(item.warehouse_id));
      const current = visible.find(
        (item) => String(item.warehouse_id) === String(session.warehouseId),
      );
      this.setData({
        warehouses: visible,
        warehouseId: current ? String(current.warehouse_id) : this.data.warehouseId,
        warehouseName: current
          ? current.warehouse_name || current.warehouse_code
          : '',
      });
    } catch {
      // The login response still carries a warehouse id, so a read failure here
      // must not block the operator from entering the work queue.
    }
  },

  onWarehouseChange(event) {
    const warehouseId = Number(event.detail.value);
    const warehouse = this.data.warehouses.find(
      (item) => item.warehouse_id === warehouseId,
    );
    const session = saveSession({
      ...this.data.session,
      warehouseId,
    });
    this.setData({
      session,
      warehouseId: String(warehouseId),
      warehouseName: warehouse
        ? warehouse.warehouse_name || warehouse.warehouse_code
        : '',
    });
  },

  openReceive() {
    if (!this.data.warehouseId) {
      wx.showToast({ title: '请先选择仓库', icon: 'none' });
      return;
    }
    wx.navigateTo({ url: '/pages/receive/index' });
  },

  openOutbound() {
    if (!this.data.warehouseId) {
      wx.showToast({ title: '请先选择仓库', icon: 'none' });
      return;
    }
    wx.navigateTo({ url: '/pages/outbound/index' });
  },

  logout() {
    wx.showModal({
      title: '退出登录',
      content: '确认退出当前账号？',
      success: (result) => {
        if (!result.confirm) return;
        clearSession();
        wx.reLaunch({ url: '/pages/login/index' });
      },
    });
  },
});
