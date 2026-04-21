/**
 * v1.4.3 keyboard fallback invariants for the shared ScanInput component.
 *
 * Source-level regression gate. Mobile vitest has no React Native runtime
 * (see mobile/src/auth/__tests__/forcedChangePersistence.test.js for the
 * pattern explanation), so we cannot render <ScanInput /> and simulate a
 * press. The user-visible behaviour was verified manually on a Chainway
 * C6000 for #103, #104, and #105.
 *
 * What this suite protects: the exact props and handlers that encode the
 * tap-to-open-keyboard, hidden-during-hardware-scan contract. If a future
 * change re-adds showSoftInputOnFocus={false}, puts back contextMenuHidden,
 * drops the onPressIn handler, or hardcodes the soft-input flag to a
 * constant, CI fails here instead of in a user's warehouse.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, it, expect, beforeAll } from 'vitest';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCAN_INPUT_PATH = resolve(__dirname, '..', 'ScanInput.js');

let source;
beforeAll(() => {
  source = readFileSync(SCAN_INPUT_PATH, 'utf8');
});

describe('ScanInput keyboard fallback (#104, #105)', () => {
  it('does not hardcode showSoftInputOnFocus to false', () => {
    // The v1.4.2-era hardcoded suppression is what blocked Fruxh's manual
    // entry in #70. Adding it back would re-break keyboard fallback.
    expect(source).not.toMatch(/showSoftInputOnFocus=\{false\}/);
    expect(source).not.toMatch(/showSoftInputOnFocus=\{\s*false\s*\}/);
  });

  it('binds showSoftInputOnFocus to the softInput state variable', () => {
    // Must be dynamic so programmatic focus (mount autoFocus and the
    // 1s refocus loop) stays silent and only user taps open the keyboard.
    expect(source).toMatch(/showSoftInputOnFocus=\{softInput\}/);
  });

  it('declares a softInput useState defaulting to false', () => {
    // Default false keeps the keyboard hidden on mount and during the
    // auto-refocus loop on the Chainway C6000.
    expect(source).toMatch(/useState\(false\)/);
    expect(source).toMatch(/\[softInput,\s*setSoftInput\]/);
  });

  it('registers an onPressIn handler on the TextInput', () => {
    // Tap is the only path that flips softInput to true; without it the
    // keyboard never opens.
    expect(source).toMatch(/onPressIn=\{handlePressIn\}/);
    expect(source).toMatch(/const\s+handlePressIn\s*=/);
  });

  it('handlePressIn flips softInput to true', () => {
    const match = source.match(/const\s+handlePressIn\s*=\s*\(\)\s*=>\s*\{[\s\S]*?\n\s*\};/);
    expect(match).not.toBeNull();
    expect(match[0]).toMatch(/setSoftInput\(true\)/);
  });

  it('registers an onBlur handler that resets softInput to false', () => {
    expect(source).toMatch(/onBlur=\{handleBlur\}/);
    const match = source.match(/const\s+handleBlur\s*=\s*\(\)\s*=>\s*\{[\s\S]*?\n\s*\};/);
    expect(match).not.toBeNull();
    expect(match[0]).toMatch(/setSoftInput\(false\)/);
  });

  it('handleSubmit resets softInput to false before the post-submit refocus', () => {
    // The 50ms post-submit refocus would re-pop the keyboard if softInput
    // stayed true. Regression gate for #105.
    const match = source.match(/const\s+handleSubmit\s*=[\s\S]*?\n\s*\};/);
    expect(match).not.toBeNull();
    const body = match[0];
    const resetIdx = body.indexOf('setSoftInput(false)');
    const refocusIdx = body.indexOf('inputRef.current?.focus()');
    expect(resetIdx).toBeGreaterThan(-1);
    expect(refocusIdx).toBeGreaterThan(-1);
    expect(resetIdx).toBeLessThan(refocusIdx);
  });
});

describe('ScanInput copy/paste support (#104)', () => {
  it('does not set contextMenuHidden on the TextInput', () => {
    // contextMenuHidden suppresses the long-press copy/paste menu. Fruxh's
    // bug report (#70) called out paste not working; removing the flag is
    // the fix. Regression gate so it does not silently come back.
    expect(source).not.toMatch(/contextMenuHidden/);
  });
});
