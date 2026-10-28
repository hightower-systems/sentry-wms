const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const app = JSON.parse(fs.readFileSync(path.join(root, 'app.json'), 'utf8'));

test('every declared mini-program page has its four source files', () => {
  for (const page of app.pages) {
    for (const extension of ['js', 'json', 'wxml', 'wxss']) {
      const filePath = path.join(root, `${page}.${extension}`);
      assert.equal(
        fs.existsSync(filePath),
        true,
        `${page}.${extension}`,
      );
      if (extension === 'json') {
        assert.doesNotThrow(() => JSON.parse(fs.readFileSync(filePath, 'utf8')));
      }
    }
  }
});

test('the mini-program contains separate receiving and outbound flows', () => {
  assert.ok(app.pages.includes('pages/receive/index'));
  assert.ok(app.pages.includes('pages/outbound/index'));
  assert.ok(app.pages.includes('pages/outbound/task'));
});

test('normal receiving and formal-outbound pages never invoke QR scanning', () => {
  for (const page of ['pages/receive/index.js', 'pages/outbound/task.js']) {
    const source = fs.readFileSync(path.join(root, page), 'utf8');
    assert.equal(source.includes('wx.scanCode'), false, page);
    assert.equal(source.includes('qr_payload'), false, page);
    assert.ok(source.includes('chooseMedia'), page);
  }
});
