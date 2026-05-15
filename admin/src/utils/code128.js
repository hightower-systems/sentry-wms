// Minimal Code 128 (subset B) encoder used by the picking ticket print
// view. Returns an array of { width, black } module segments suitable
// for rendering as SVG <rect> elements. Kept inline to avoid a runtime
// dependency on a barcode library.
//
// Reference: GS1 / ISO/IEC 15417 Code 128 symbology. Subset B covers
// ASCII 32-127, which is sufficient for order numbers (digits + a few
// punctuation chars). Patterns are encoded as digit strings where each
// digit is the module width of an alternating bar/space starting with
// a bar.

const PATTERNS = [
  '212222', '222122', '222221', '121223', '121322', '131222', '122213',
  '122312', '132212', '221213', '221312', '231212', '112232', '122132',
  '122231', '113222', '123122', '123221', '223211', '221132', '221231',
  '213212', '223112', '312131', '311222', '321122', '321221', '312212',
  '322112', '322211', '212123', '212321', '232121', '111323', '131123',
  '131321', '112313', '132113', '132311', '211313', '231113', '231311',
  '112133', '112331', '132131', '113123', '113321', '133121', '313121',
  '211331', '231131', '213113', '213311', '213131', '311123', '311321',
  '331121', '312113', '312311', '332111', '314111', '221411', '431111',
  '111224', '111422', '121124', '121421', '141122', '141221', '112214',
  '112412', '122114', '122411', '142112', '142211', '241211', '221114',
  '413111', '241112', '134111', '111242', '121142', '121241', '114212',
  '124112', '124211', '411212', '421112', '421211', '212141', '214121',
  '412121', '111143', '111341', '131141', '114113', '114311', '411113',
  '411311', '113141', '114131', '311141', '411131', '211412', '211214',
  '211232',
];
const STOP_PATTERN = '2331112';
const START_B = 104;

export function encodeCode128B(text) {
  if (!text) return { segments: [], totalModules: 0 };
  const values = [START_B];
  for (const ch of text) {
    const code = ch.charCodeAt(0);
    // Code 128 subset B covers ASCII 32-127. Fall back to '?' for
    // anything outside that range so a stray unicode glyph in a memo
    // never throws.
    const value = code >= 32 && code <= 127 ? code - 32 : '?'.charCodeAt(0) - 32;
    values.push(value);
  }
  let checksum = START_B;
  for (let i = 1; i < values.length; i++) {
    checksum += values[i] * i;
  }
  checksum %= 103;
  values.push(checksum);

  const segments = [];
  let totalModules = 0;
  for (const v of values) {
    const pattern = PATTERNS[v];
    for (let i = 0; i < pattern.length; i++) {
      const width = Number(pattern[i]);
      segments.push({ width, black: i % 2 === 0 });
      totalModules += width;
    }
  }
  for (let i = 0; i < STOP_PATTERN.length; i++) {
    const width = Number(STOP_PATTERN[i]);
    segments.push({ width, black: i % 2 === 0 });
    totalModules += width;
  }
  return { segments, totalModules };
}
