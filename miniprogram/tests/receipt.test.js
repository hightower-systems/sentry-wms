const test = require('node:test');
const assert = require('node:assert/strict');

const {
  remainingQuantity,
  scanProgress,
  validateReceiptDraft,
} = require('../utils/receipt');

function ticket(overrides = {}) {
  return {
    declared_quantity: '4.000',
    received_quantity: '0.000',
    expected_pallet_count: 2,
    pallet_count: 0,
    ...overrides,
  };
}

test('allows a genuine short receipt with fewer pallets and a reason', () => {
  const result = validateReceiptDraft({
    ticket: ticket(),
    scans: [{ suffix: '1-1' }],
    quantity: '2',
    differenceReason: '少 2 件，余货明天补到',
  });

  assert.equal(result.ok, true);
  assert.equal(result.progress.registered, 1);
  assert.equal(result.progress.remaining, 1);
});

test('requires every pallet when the whole ticket quantity arrives', () => {
  const result = validateReceiptDraft({
    ticket: ticket(),
    scans: [{ suffix: '1-1' }],
    quantity: '4',
    differenceReason: '',
  });

  assert.equal(result.ok, false);
  assert.match(result.error, /托盘尚未齐套/);
});

test('uses the remaining quantity on a follow-up receipt', () => {
  const partial = ticket({
    received_quantity: '2.000',
    pallet_count: 1,
  });
  assert.equal(remainingQuantity(partial), 2);
  assert.equal(scanProgress(partial, []).text, '1 / 2 托');

  const result = validateReceiptDraft({
    ticket: partial,
    scans: [{ suffix: '1-2' }],
    quantity: '2',
    differenceReason: '',
  });

  assert.equal(result.ok, true);
});

test('rejects more quantity than remains without inventing a reason', () => {
  const result = validateReceiptDraft({
    ticket: ticket({ received_quantity: '3.000' }),
    scans: [{ suffix: '1-2' }],
    quantity: '2',
    differenceReason: '',
  });

  assert.equal(result.ok, false);
  assert.match(result.error, /剩余应收 1/);
});
