/**
 * JS bridge for the ChainwayScanner native module.
 *
 * On Android this talks to ChainwayScannerModule.java which
 * registers a BroadcastReceiver for Chainway C6000 scan intents.
 *
 * On iOS / Expo Go / web this is a harmless no-op.
 */

import { NativeModules, NativeEventEmitter, Platform } from 'react-native';

const { ChainwayScanner: NativeModule } = NativeModules;

// If the native module is not available (Expo Go, iOS, web), provide a stub
const isAvailable = Platform.OS === 'android' && NativeModule != null;

const emitter = isAvailable ? new NativeEventEmitter(NativeModule) : null;

const ChainwayScanner = {
  /**
   * Whether the native module is available on this device.
   */
  isAvailable,

  /**
   * Start listening for broadcast scan intents.
   * @param {string} [action]  - Intent action (null = use default)
   * @param {string} [extraKey] - Intent extra key for barcode (null = use default)
   */
  startListening(action = null, extraKey = null) {
    if (!isAvailable) return;
    NativeModule.startListening(action, extraKey);
  },

  /**
   * Stop listening for broadcast scan intents.
   */
  stopListening() {
    if (!isAvailable) return;
    NativeModule.stopListening();
  },

  /**
   * Subscribe to barcode scan events.
   * @param {(barcode: string) => void} callback
   * @returns {{ remove: () => void }} subscription (call .remove() to unsubscribe)
   */
  addListener(callback) {
    if (!emitter) return { remove: () => {} };
    return emitter.addListener('onBarcodeScan', (event) => {
      const barcode = typeof event === 'string' ? event : event?.barcode;
      if (barcode) callback(barcode);
    });
  },
};

export default ChainwayScanner;
