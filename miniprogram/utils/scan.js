const SUFFIX_PATTERN = /^[1-9]\d*-[1-9]\d*$/;

function normalizeSuffix(value) {
  const text = String(value || '').trim();
  return SUFFIX_PATTERN.test(text) ? text : '';
}

function parseQrPayload(raw) {
  const payload = String(raw || '').trim();
  if (!payload) return {};
  try {
    const parsed = JSON.parse(payload);
    if (parsed && typeof parsed === 'object') {
      return normalizePayload(parsed);
    }
  } catch {
    // Factory labels may be plain text or key/value text.
  }

  const result = {};
  const pairs = payload.split(/[|;,\n]+/);
  pairs.forEach((pair) => {
    const match = pair.match(/^\s*([^:=]+)\s*[:=]\s*(.+?)\s*$/);
    if (!match) return;
    const key = normalizeKey(match[1]);
    if (key) result[key] = match[2].trim();
  });

  const suffixMatch = payload.match(
    /(?:托盘后缀|吊牌后缀|tag[_\s-]?suffix|suffix)\s*[:=]\s*([1-9]\d*-[1-9]\d*)/i,
  );
  if (suffixMatch) result.tag_suffix = suffixMatch[1];
  if (!result.tag_suffix) {
    const standalone = payload.match(/(?:^|[\s|,;])([1-9]\d*-[1-9]\d*)(?:$|[\s|,;])/);
    if (standalone) result.tag_suffix = standalone[1];
  }
  return result;
}

function normalizeKey(value) {
  const key = String(value || '').trim().toLowerCase().replace(/\s+/g, '');
  const aliases = {
    船号: 'ship_no',
    工程号: 'engineering_no',
    制作图号: 'production_drawing_no',
    制作图编号: 'production_drawing_no',
    物料编码: 'material_code',
    物资编码: 'material_code',
    品名: 'item_name',
    物料名称: 'item_name',
    厂家: 'manufacturer_name',
    厂家名称: 'manufacturer_name',
    安装托盘号: 'installation_pallet_no',
    安装托盘名称: 'installation_pallet_name',
    目的地: 'destination',
    批次号: 'batch_no',
    批次: 'batch_no',
    托盘后缀: 'tag_suffix',
    吊牌后缀: 'tag_suffix',
    数量: 'label_quantity',
    件数: 'label_quantity',
    二维码托盘号: 'qr_pallet_no',
    托盘号: 'qr_pallet_no',
    ship_no: 'ship_no',
    engineering_no: 'engineering_no',
    production_drawing_no: 'production_drawing_no',
    material_code: 'material_code',
    item_name: 'item_name',
    material_name: 'item_name',
    batch_no: 'batch_no',
    manufacturer_name: 'manufacturer_name',
    installation_pallet_no: 'installation_pallet_no',
    installation_pallet_name: 'installation_pallet_name',
    destination: 'destination',
    pallet_no: 'qr_pallet_no',
    pallet: 'qr_pallet_no',
    batch: 'batch_no',
    quantity: 'label_quantity',
    suffix: 'tag_suffix',
    tag_suffix: 'tag_suffix',
  };
  return aliases[key] || '';
}

function normalizePayload(payload) {
  const result = {};
  Object.entries(payload).forEach(([key, value]) => {
    const normalizedKey = normalizeKey(key);
    if (!normalizedKey || value === null || value === undefined || value === '') {
      return;
    }
    result[normalizedKey] = String(value).trim();
  });
  return result;
}

function chooseNextSuffix(expected, completed) {
  const done = new Set(completed || []);
  return (expected || []).find((suffix) => !done.has(suffix)) || '';
}

module.exports = {
  chooseNextSuffix,
  normalizePayload,
  normalizeSuffix,
  parseQrPayload,
};
