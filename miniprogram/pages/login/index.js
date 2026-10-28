const { request } = require('../../utils/request');
const {
  getApiBase,
  getSession,
  normalizeApiBase,
  saveSession,
} = require('../../utils/session');

Page({
  data: {
    apiBase: '',
    username: '',
    password: '',
    busy: false,
  },

  onLoad() {
    const existing = getSession();
    if (existing) {
      wx.reLaunch({ url: '/pages/home/index' });
      return;
    }
    this.setData({ apiBase: getApiBase() });
  },

  onApiBaseInput(event) {
    this.setData({ apiBase: event.detail.value });
  },

  onUsernameInput(event) {
    this.setData({ username: event.detail.value });
  },

  onPasswordInput(event) {
    this.setData({ password: event.detail.value });
  },

  async login() {
    const apiBase = normalizeApiBase(this.data.apiBase);
    if (!apiBase || !this.data.username.trim() || !this.data.password) {
      wx.showToast({ title: '请填写服务器、账号和密码', icon: 'none' });
      return;
    }
    this.setData({ busy: true });
    try {
      const response = await request({
        path: '/auth/login',
        method: 'POST',
        data: {
          username: this.data.username.trim(),
          password: this.data.password,
        },
        apiBase,
      });
      saveSession({
        apiBase,
        token: response.token,
        user: response.user,
        warehouseId: response.user.warehouse_id
          || (response.user.warehouse_ids || [])[0]
          || null,
        loginTimestamp: Date.now(),
      });
      if (response.user.must_change_password) {
        wx.showModal({
          title: '需要修改密码',
          content: '该账号仍使用临时密码，请先在管理端完成密码修改。',
          showCancel: false,
          success: () => wx.reLaunch({ url: '/pages/home/index' }),
        });
      } else {
        wx.reLaunch({ url: '/pages/home/index' });
      }
    } catch (error) {
      wx.showToast({ title: error.message, icon: 'none' });
    } finally {
      this.setData({ busy: false });
    }
  },
});
