/**
 * Expo config plugin: Chainway Scanner Broadcast Receiver
 *
 * Injects a native Android module that listens for barcode scan
 * broadcast intents from the Chainway C6000 scanner hardware.
 *
 * The module exposes events to JS via DeviceEventEmitter so
 * React Native can receive scans without keyboard wedge.
 */

const {
  withDangerousMod,
  withMainApplication,
} = require("expo/config-plugins");
const fs = require("fs");
const path = require("path");

const PACKAGE = "com.hightowersystems.sentrywms";
const PACKAGE_DIR = PACKAGE.replace(/\./g, "/");

// ── Java source files ──────────────────────────────────────

const SCANNER_MODULE_JAVA = `package ${PACKAGE};

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.util.Log;

import com.facebook.react.bridge.Arguments;
import com.facebook.react.bridge.ReactApplicationContext;
import com.facebook.react.bridge.ReactContextBaseJavaModule;
import com.facebook.react.bridge.ReactMethod;
import com.facebook.react.bridge.WritableMap;
import com.facebook.react.modules.core.DeviceEventManagerModule;

/**
 * Native module that registers a BroadcastReceiver for Chainway C6000
 * barcode scan intents and forwards them to JS via DeviceEventEmitter.
 *
 * Default intent action: com.chainway.sdk.barcode.BARCODE_DECODING_DATA
 * Default extra key:     BARCODE_DATA_EXTRA
 *
 * Both are configurable at runtime from JS so the user can match
 * whatever their Chainway scanner app is configured to broadcast.
 */
public class ChainwayScannerModule extends ReactContextBaseJavaModule {

    private static final String TAG = "ChainwayScanner";
    private static final String EVENT_NAME = "onBarcodeScan";

    // Defaults  -  overridden by startListening() params
    private String intentAction = "com.chainway.sdk.barcode.BARCODE_DECODING_DATA";
    private String extraKey = "BARCODE_DATA_EXTRA";

    private BroadcastReceiver receiver;
    private boolean listening = false;

    public ChainwayScannerModule(ReactApplicationContext ctx) {
        super(ctx);
    }

    @Override
    public String getName() {
        return "ChainwayScanner";
    }

    /**
     * Start listening for broadcast intents.
     * @param action  Intent action string (nullable  -  uses default)
     * @param extra   Intent extra key for barcode data (nullable  -  uses default)
     */
    @ReactMethod
    public void startListening(String action, String extra) {
        if (listening) {
            Log.d(TAG, "Already listening  -  call stopListening first");
            return;
        }

        if (action != null && !action.isEmpty()) intentAction = action;
        if (extra != null && !extra.isEmpty()) extraKey = extra;

        Log.d(TAG, "Registering receiver: action=" + intentAction + " extra=" + extraKey);

        receiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                String barcode = intent.getStringExtra(extraKey);
                if (barcode == null || barcode.isEmpty()) {
                    // Try common alternative extra keys
                    barcode = intent.getStringExtra("barcode_string");
                    if (barcode == null) barcode = intent.getStringExtra("scannerdata");
                    if (barcode == null) barcode = intent.getStringExtra("data");
                    if (barcode == null) barcode = intent.getStringExtra("SCAN_BARCODE1");
                    if (barcode == null) barcode = intent.getStringExtra("decode_data");
                }
                if (barcode != null && !barcode.isEmpty()) {
                    Log.d(TAG, "Scan received: " + barcode);
                    sendEvent(barcode);
                } else {
                    Log.w(TAG, "Broadcast received but no barcode data found in extras");
                }
            }
        };

        IntentFilter filter = new IntentFilter(intentAction);
        ReactApplicationContext ctx = getReactApplicationContext();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ctx.registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED);
        } else {
            ctx.registerReceiver(receiver, filter);
        }
        listening = true;
        Log.d(TAG, "Receiver registered");
    }

    @ReactMethod
    public void stopListening() {
        if (!listening || receiver == null) {
            Log.d(TAG, "Not listening  -  nothing to stop");
            return;
        }
        try {
            getReactApplicationContext().unregisterReceiver(receiver);
        } catch (Exception e) {
            Log.w(TAG, "Error unregistering receiver", e);
        }
        receiver = null;
        listening = false;
        Log.d(TAG, "Receiver unregistered");
    }

    @ReactMethod
    public void isListening(com.facebook.react.bridge.Promise promise) {
        promise.resolve(listening);
    }

    /** Required for DeviceEventEmitter in new arch */
    @ReactMethod
    public void addListener(String eventName) { /* no-op */ }
    @ReactMethod
    public void removeListeners(int count) { /* no-op */ }

    private void sendEvent(String barcode) {
        ReactApplicationContext ctx = getReactApplicationContext();
        if (ctx.hasActiveReactInstance()) {
            WritableMap params = Arguments.createMap();
            params.putString("barcode", barcode);
            ctx.getJSModule(DeviceEventManagerModule.RCTDeviceEventEmitter.class)
               .emit(EVENT_NAME, params);
        }
    }
}
`;

const SCANNER_PACKAGE_JAVA = `package ${PACKAGE};

import com.facebook.react.ReactPackage;
import com.facebook.react.bridge.NativeModule;
import com.facebook.react.bridge.ReactApplicationContext;
import com.facebook.react.uimanager.ViewManager;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public class ChainwayScannerPackage implements ReactPackage {
    @Override
    public List<NativeModule> createNativeModules(ReactApplicationContext ctx) {
        List<NativeModule> modules = new ArrayList<>();
        modules.add(new ChainwayScannerModule(ctx));
        return modules;
    }

    @Override
    public List<ViewManager> createViewManagers(ReactApplicationContext ctx) {
        return Collections.emptyList();
    }
}
`;

// ── Plugin implementation ──────────────────────────────────

function withChainwayScannerFiles(config) {
  return withDangerousMod(config, [
    "android",
    async (cfg) => {
      const projectRoot = cfg.modRequest.projectRoot;
      const javaDir = path.join(
        projectRoot,
        "android",
        "app",
        "src",
        "main",
        "java",
        ...PACKAGE_DIR.split("/")
      );

      fs.mkdirSync(javaDir, { recursive: true });

      fs.writeFileSync(
        path.join(javaDir, "ChainwayScannerModule.java"),
        SCANNER_MODULE_JAVA
      );
      fs.writeFileSync(
        path.join(javaDir, "ChainwayScannerPackage.java"),
        SCANNER_PACKAGE_JAVA
      );

      console.log("[chainway-scanner] Wrote native module files to", javaDir);
      return cfg;
    },
  ]);
}

function withChainwayScannerPackageRegistration(config) {
  return withMainApplication(config, (cfg) => {
    const contents = cfg.modResults.contents;

    // Add import if not present
    const importLine = `import ${PACKAGE}.ChainwayScannerPackage;`;
    if (!contents.includes(importLine)) {
      cfg.modResults.contents = contents.replace(
        /^(package .+;\n)/m,
        `$1\n${importLine}\n`
      );
    }

    // Add to getPackages() if not present
    if (!cfg.modResults.contents.includes("ChainwayScannerPackage")) {
      cfg.modResults.contents = cfg.modResults.contents.replace(
        /(packages\.add\(new \w+\(\)\);)/,
        `$1\n          packages.add(new ChainwayScannerPackage());`
      );
    }

    // Fallback: if the above pattern didn't match (newer RN uses a different pattern),
    // try the PackageList approach
    if (!cfg.modResults.contents.includes("ChainwayScannerPackage")) {
      cfg.modResults.contents = cfg.modResults.contents.replace(
        /(return packages;)/,
        `packages.add(new ChainwayScannerPackage());\n          $1`
      );
    }

    console.log("[chainway-scanner] Registered package in MainApplication");
    return cfg;
  });
}

module.exports = (config) => {
  config = withChainwayScannerFiles(config);
  config = withChainwayScannerPackageRegistration(config);
  return config;
};
