const {
  clearSession,
  getApiBase,
  getSession,
} = require('./session');

const FIELD_LABELS = {
  material_code: '物料编码',
  item_name: '品名',
  tag_suffix: '托盘后缀',
  qr_payload: '二维码原文',
  ship_no: '船号',
  engineering_no: '工程号',
  production_drawing_no: '制作图号',
  batch_no: '批次号',
  label_quantity: '吊牌件数',
  received_quantity: '实收数量',
};

function detailMessage(payload) {
  if (Array.isArray(payload?.details) && payload.details.length) {
    return payload.details
      .slice(0, 3)
      .map((detail) => {
        const field = detail.loc?.[0];
        const label = FIELD_LABELS[field] || field || '字段';
        const reason = (
          detail.type === 'missing'
          || /field required/i.test(detail.msg || '')
        )
          ? '必填'
          : (detail.msg || '填写不正确');
        return `${label}：${reason}`;
      })
      .join('；');
  }
  if (Array.isArray(payload?.mismatches) && payload.mismatches.length) {
    return payload.mismatches
      .slice(0, 3)
      .map((item) => {
        const label = FIELD_LABELS[item.field] || item.field || '吊牌字段';
        if (item.reason === 'REQUIRED') {
          return `${label}未识别`;
        }
        return `${label}不一致（应为 ${item.expected || '—'}，识别为 ${item.actual || '—'}）`;
      })
      .join('；');
  }
  return '';
}

function errorMessage(payload, fallback) {
  if (!payload) return fallback;
  const details = detailMessage(payload);
  if (payload.message) {
    return details ? `${payload.message}：${details}` : payload.message;
  }
  if (details) return details;
  if (payload.error) return payload.error;
  return fallback;
}

function requestError(payload, fallback, statusCode) {
  const error = new Error(errorMessage(payload, fallback));
  error.code = payload?.error || '';
  error.statusCode = statusCode;
  error.response = {
    statusCode,
    data: payload,
  };
  return error;
}

function request(options) {
  const session = getSession();
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${options.apiBase || getApiBase()}${options.path}`,
      method: options.method || 'GET',
      data: options.data,
      timeout: options.timeout || 15000,
      header: {
        'content-type': 'application/json',
        ...(session ? { Authorization: `Bearer ${session.token}` } : {}),
        ...(options.header || {}),
      },
      success(response) {
        if (response.statusCode === 401) {
          clearSession();
          wx.reLaunch({ url: '/pages/login/index' });
          reject(new Error('登录已过期，请重新登录'));
          return;
        }
        if (response.statusCode < 200 || response.statusCode >= 300) {
          reject(requestError(
            response.data,
            `请求失败 (${response.statusCode})`,
            response.statusCode,
          ));
          return;
        }
        resolve(response.data);
      },
      fail(error) {
        reject(new Error(error.errMsg || '网络连接失败'));
      },
    });
  });
}

function upload(options) {
  const session = getSession();
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: `${getApiBase()}${options.path}`,
      filePath: options.filePath,
      name: 'file',
      formData: options.formData || {},
      timeout: options.timeout || 30000,
      header: session ? { Authorization: `Bearer ${session.token}` } : {},
      success(response) {
        let data = {};
        try {
          data = JSON.parse(response.data || '{}');
        } catch {
          data = { message: response.data };
        }
        if (response.statusCode === 401) {
          clearSession();
          wx.reLaunch({ url: '/pages/login/index' });
          reject(new Error('登录已过期，请重新登录'));
          return;
        }
        if (response.statusCode < 200 || response.statusCode >= 300) {
          reject(requestError(
            data,
            `上传失败 (${response.statusCode})`,
            response.statusCode,
          ));
          return;
        }
        resolve(data);
      },
      fail(error) {
        reject(new Error(error.errMsg || '照片上传失败'));
      },
    });
  });
}

module.exports = {
  request,
  upload,
};
