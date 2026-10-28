function toNumber(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number : 0;
}

function remainingQuantity(ticket) {
  return Math.max(
    toNumber(ticket?.declared_quantity) - toNumber(ticket?.received_quantity),
    0,
  );
}

function scanProgress(ticket, scans) {
  const expected = toNumber(ticket?.expected_pallet_count);
  const registered = toNumber(ticket?.pallet_count) + (scans?.length || 0);
  const remaining = expected ? Math.max(expected - registered, 0) : null;
  return {
    expected,
    registered,
    remaining,
    text: expected
      ? `${registered} / ${expected} 托`
      : `已登记 ${registered} 托`,
  };
}

function validateReceiptDraft({
  ticket,
  scans,
  quantity,
  differenceReason,
}) {
  if (!ticket) return { ok: false, error: '请先选择收货任务' };
  if (!scans?.length) return { ok: false, error: '请先拍摄并识别吊牌' };

  const remaining = remainingQuantity(ticket);
  const receivedNow = toNumber(quantity);
  if (receivedNow <= 0) {
    return { ok: false, error: '请填写本次实收数量' };
  }
  if (receivedNow > remaining) {
    return {
      ok: false,
      error: `本次实收数量不能超过剩余应收 ${remaining}`,
    };
  }
  if (receivedNow < remaining && !String(differenceReason || '').trim()) {
    return {
      ok: false,
      error: '本次实收少于应收数量，请填写差异原因',
    };
  }

  const progress = scanProgress(ticket, scans);
  if (progress.expected && progress.registered > progress.expected) {
    return {
      ok: false,
      error: `本次识别托盘超过预计数量，预计 ${progress.expected} 托`,
    };
  }

  const willBeComplete = (
    toNumber(ticket.received_quantity) + receivedNow
    >= toNumber(ticket.declared_quantity)
  );
  if (
    willBeComplete
    && progress.expected
    && progress.registered < progress.expected
  ) {
    return {
      ok: false,
      error: `整票数量已到齐，但托盘尚未齐套，还缺 ${progress.expected - progress.registered} 托`,
    };
  }

  return { ok: true, error: '', progress };
}

module.exports = {
  remainingQuantity,
  scanProgress,
  validateReceiptDraft,
};
