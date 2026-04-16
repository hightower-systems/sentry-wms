/**
 * Expo config plugin: enable android:usesCleartextTraffic
 *
 * Required for HTTP (non-HTTPS) connections on Android 9+.
 * Warehouse devices typically use local IPs over HTTP.
 */
const { withAndroidManifest } = require("expo/config-plugins");

module.exports = function withCleartextTraffic(config) {
  return withAndroidManifest(config, (cfg) => {
    const app = cfg.modResults.manifest.application[0];
    app.$["android:usesCleartextTraffic"] = "true";
    return cfg;
  });
};
