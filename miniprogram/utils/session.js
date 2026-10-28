const { defaultApiBase, sessionTimeoutMs } = require('../config');

const SESSION_KEY = 'shipyard_session';

function getSession() {
  const session = wx.getStorageSync(SESSION_KEY);
  if (!session || !session.token) return null;
  if (
    session.loginTimestamp
    && Date.now() - Number(session.loginTimestamp) > sessionTimeoutMs
  ) {
    wx.removeStorageSync(SESSION_KEY);
    return null;
  }
  return session;
}

function saveSession(session) {
  const next = {
    ...session,
    apiBase: normalizeApiBase(session.apiBase),
    loginTimestamp: session.loginTimestamp || Date.now(),
  };
  wx.setStorageSync(SESSION_KEY, next);
  const app = getApp();
  if (app) app.globalData.session = next;
  return next;
}

function clearSession() {
  wx.removeStorageSync(SESSION_KEY);
  const app = getApp();
  if (app) app.globalData.session = null;
}

function normalizeApiBase(value) {
  return (value || defaultApiBase).trim().replace(/\/+$/, '');
}

function getApiBase() {
  const session = getSession();
  return session ? session.apiBase : defaultApiBase;
}

function requireSession() {
  const session = getSession();
  if (!session) {
    wx.reLaunch({ url: '/pages/login/index' });
    return null;
  }
  return session;
}

module.exports = {
  clearSession,
  getApiBase,
  getSession,
  normalizeApiBase,
  requireSession,
  saveSession,
};
