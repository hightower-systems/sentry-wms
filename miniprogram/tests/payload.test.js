const test = require('node:test');
const assert = require('node:assert/strict');

const {
  buildOutboundConfirmation,
  buildReceiptConfirmation,
} = require('../utils/payload');

test('outbound confirmation requires every local evidence item', () => {
  const body = buildOutboundConfirmation(
    'outbound-1',
    [
      {
        suffix: '1-1',
        recognition: { recognition_id: 'recognition-1' },
        photo: { photo_id: 'photo-1' },
      },
      {
        suffix: '1-2',
        recognition: { recognition_id: 'recognition-2' },
        photo: { photo_id: 'photo-2' },
      },
    ],
    'WX-1',
  );

  assert.equal(body.pallets.length, 2);
  assert.deepEqual(
    body.pallets.map((item) => item.photo_id),
    ['photo-1', 'photo-2'],
  );
  assert.deepEqual(
    body.pallets.map((item) => item.label_recognition_id),
    ['recognition-1', 'recognition-2'],
  );
  assert.equal(body.pallets[0].qr_payload, undefined);
});

test('receipt confirmation carries shortage reason without changing quantity', () => {
  const body = buildReceiptConfirmation({
    idempotencyKey: 'receipt-1',
    quantity: 3,
    evidence: [
      {
        suffix: '1-1',
        recognition: { recognition_id: 'recognition-1' },
        photo: { photo_id: 'photo-1' },
      },
    ],
    differenceReason: '少 1 件',
    deviceId: 'WX-1',
  });

  assert.equal(body.received_quantity, 3);
  assert.equal(body.difference_reason, '少 1 件');
});
