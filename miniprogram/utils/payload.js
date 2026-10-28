function buildOutboundConfirmation(idempotencyKey, evidence, deviceId) {
  return {
    idempotency_key: idempotencyKey,
    client_device_id: deviceId,
    pallets: evidence.map((item) => ({
      tag_suffix: item.suffix,
      label_recognition_id: item.recognition.recognition_id,
      photo_id: item.photo.photo_id,
    })),
  };
}

function buildReceiptConfirmation({
  idempotencyKey,
  quantity,
  evidence,
  differenceReason,
  deviceId,
}) {
  const body = {
    idempotency_key: idempotencyKey,
    received_quantity: Number(quantity),
    client_device_id: deviceId,
    pallets: evidence.map((item) => ({
      tag_suffix: item.suffix,
      label_recognition_id: item.recognition.recognition_id,
      photo_id: item.photo.photo_id,
    })),
  };
  if (differenceReason) body.difference_reason = differenceReason;
  return body;
}

module.exports = {
  buildOutboundConfirmation,
  buildReceiptConfirmation,
};
