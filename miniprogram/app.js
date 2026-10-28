const { getSession } = require('./utils/session');

App({
  globalData: {
    session: null,
  },

  onLaunch() {
    this.globalData.session = getSession();
  },
});
