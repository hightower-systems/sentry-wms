/**
 * Expo config plugin: enable android:usesCleartextTraffic
 *
 * Required for HTTP (non-HTTPS) connections on Android 9+.
 * Warehouse devices typically use local IPs over HTTP.
 *
 * Only enables cleartext for non-production builds. Production builds
 * enforce HTTPS by leaving usesCleartextTraffic as false in app.json.
 */
const { withAndroidManifest } = require("expo/config-plugins");

module.exports = function withCleartextTraffic(config) {
  return withAndroidManifest(config, (cfg) => {
    const profile = process.env.EAS_BUILD_PROFILE || "development";
    if (profile !== "production") {
      const app = cfg.modResults.manifest.application[0];
      app.$["android:usesCleartextTraffic"] = "true";
    }
    return cfg;
  });
};
