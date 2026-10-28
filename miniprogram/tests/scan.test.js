const test = require('node:test');
const assert = require('node:assert/strict');

const {
  chooseNextSuffix,
  normalizeSuffix,
  parseQrPayload,
} = require('../utils/scan');

test('parses JSON factory label payloads', () => {
  const payload = parseQrPayload(JSON.stringify({
    工程号: 'H1613',
    托盘号: '132ME201EM',
    批次号: 'H1613-B01',
    托盘后缀: '1-2',
  }));

  assert.deepEqual(payload, {
    engineering_no: 'H1613',
    qr_pallet_no: '132ME201EM',
    batch_no: 'H1613-B01',
    tag_suffix: '1-2',
  });
});

test('parses key-value and standalone suffix text', () => {
  assert.equal(
    parseQrPayload('工程号=H1613|托盘号=P-1|批次号=B-01|1-2').tag_suffix,
    '1-2',
  );
  assert.equal(
    parseQrPayload('suffix: 2-4').tag_suffix,
    '2-4',
  );
});

test('rejects malformed suffix and chooses next whole-ticket pallet', () => {
  assert.equal(normalizeSuffix('1-0'), '');
  assert.equal(normalizeSuffix('1-2'), '1-2');
  assert.equal(chooseNextSuffix(['1-1', '1-2'], ['1-1']), '1-2');
  assert.equal(chooseNextSuffix(['1-1'], ['1-1']), '');
});
