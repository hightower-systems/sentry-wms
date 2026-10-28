const test = require('node:test');
const assert = require('node:assert/strict');

const { request } = require('../utils/request');

test('keeps API details and turns them into a field-level recovery message', async () => {
  global.wx = {
    getStorageSync: () => ({
      token: 'test-token',
      apiBase: 'http://api.test/api',
      loginTimestamp: Date.now(),
    }),
    request: (options) => {
      options.success({
        statusCode: 400,
        data: {
          error: 'validation_error',
          details: [
            {
              loc: ['material_code'],
              msg: 'Field required',
            },
            {
              loc: ['item_name'],
              msg: 'Field required',
            },
          ],
        },
      });
    },
  };

  await assert.rejects(
    request({
      path: '/shipyard/outbound/tasks/task-1/scan',
      method: 'POST',
      data: {},
    }),
    (error) => {
      assert.match(error.message, /物料编码：必填/);
      assert.match(error.message, /品名：必填/);
      assert.equal(error.statusCode, 400);
      assert.equal(error.response.data.error, 'validation_error');
      return true;
    },
  );
});
