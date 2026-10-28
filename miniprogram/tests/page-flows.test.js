const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

function installMock(relativePath, exports) {
  const modulePath = require.resolve(path.resolve(__dirname, '..', relativePath));
  require.cache[modulePath] = {
    id: modulePath,
    filename: modulePath,
    loaded: true,
    exports,
  };
}

function loadPage(relativePath) {
  let definition;
  global.Page = (config) => {
    definition = config;
  };
  const pagePath = require.resolve(path.resolve(__dirname, '..', relativePath));
  delete require.cache[pagePath];
  require(pagePath);
  return definition;
}

function instantiate(definition) {
  const page = {
    ...definition,
    data: JSON.parse(JSON.stringify(definition.data)),
    setData(patch) {
      this.data = { ...this.data, ...patch };
    },
  };
  return page;
}

test('receiving captures once and uses OCR without scanning a QR code', async () => {
  const uploads = [];
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => {
      throw new Error('normal receiving must not call the legacy scan API');
    },
    upload: async (options) => {
      uploads.push(options);
      return {
        matched: true,
        needs_suffix: false,
        recognized: {
          ship_no: 'HUX-01',
          material_code: 'MAT-01',
          item_name: '船用支架',
          tag_suffix: '1-1',
        },
        mismatches: [],
        recognition: { recognition_id: 'recognition-1' },
        photo: { photo_id: 'photo-1', file_name: 'label.jpg' },
      };
    },
  });

  global.wx = {
    chooseMedia: async () => ({
      tempFiles: [{ tempFilePath: '/tmp/label.jpg' }],
    }),
    showToast: () => {},
    showModal: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedTicket = {
    ticket_id: 'ticket-1',
    declared_quantity: '4.000',
    received_quantity: '0.000',
    expected_pallet_count: 2,
    pallet_count: 0,
  };

  await page.captureLabel();

  assert.equal(uploads.length, 1);
  assert.equal(
    uploads[0].path,
    '/shipyard/inbound/tickets/ticket-1/label-recognition',
  );
  assert.equal(page.data.scans.length, 1);
  assert.equal(page.data.scans[0].recognition.recognition_id, 'recognition-1');
  assert.equal(page.data.palletProgressText, '1 / 2 托');
  assert.equal(page.data.nextSuffix, '还差 1 托');
});

test('receiving submits a short quantity with fewer pallets and its reason', async () => {
  const requests = [];
  const session = {
    user: { username: 'receiver' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async (options) => {
      requests.push(options);
      return {
        ticket: {
          item_name: '船用支架',
          received_quantity: '2.000',
        },
        receipt: {
          location_path_snapshot: 'A 区 / A1',
        },
      };
    },
    upload: async () => ({ photo: { photo_id: 'photo-1' } }),
  });
  global.wx = {
    showToast: () => {},
    showModal: () => {},
  };

  const page = instantiate(loadPage('pages/receive/index.js'));
  page.data.session = session;
  page.data.selectedTicket = {
    ticket_id: 'ticket-1',
    declared_quantity: '4.000',
    received_quantity: '0.000',
    expected_pallet_count: 2,
    pallet_count: 0,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];
  page.data.quantity = '2';
  page.data.differenceReason = '少 2 件，余货明天补到';

  await page.confirmReceipt();

  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/shipyard/inbound/tickets/ticket-1/receipts');
  assert.equal(requests[0].data.received_quantity, 2);
  assert.equal(requests[0].data.pallets.length, 1);
  assert.equal(requests[0].data.pallets[0].label_recognition_id, 'recognition-1');
  assert.equal(requests[0].data.pallets[0].qr_payload, undefined);
  assert.equal(requests[0].data.difference_reason, '少 2 件，余货明天补到');
});

test('outbound asks only for the remaining suffix when OCR misses it', async () => {
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({}),
    upload: async () => ({
      matched: false,
      needs_suffix: true,
      recognized: {
        material_code: 'MAT-01',
        item_name: '船用支架',
      },
      mismatches: [{ field: 'tag_suffix', reason: 'REQUIRED' }],
      recognition: { recognition_id: 'recognition-2' },
      photo: { photo_id: 'photo-2', file_name: 'label.jpg' },
    }),
  });
  global.wx = {
    chooseMedia: async () => ({
      tempFiles: [{ tempFilePath: '/tmp/label.jpg' }],
    }),
    showToast: () => {},
    showModal: () => {},
  };

  const page = instantiate(loadPage('pages/outbound/task.js'));
  page.data.session = session;
  page.data.task = {
    task_id: 'task-1',
    status: 'PENDING',
    expected_tag_suffixes: ['1-1', '1-2'],
    expected_pallet_count: 2,
  };
  page.data.scans = [{
    suffix: '1-1',
    recognition: { recognition_id: 'recognition-1' },
    photo: { photo_id: 'photo-1' },
  }];

  await page.captureLabel();

  assert.equal(page.data.suffixPickerVisible, true);
  assert.deepEqual(page.data.suffixChoices, ['1-2']);
  assert.equal(page.data.scans.length, 1);

  page.selectSuffix({ currentTarget: { dataset: { suffix: '1-2' } } });

  assert.equal(page.data.suffixPickerVisible, false);
  assert.equal(page.data.scans.length, 2);
  assert.equal(page.data.scans[1].recognition.recognition_id, 'recognition-2');
  assert.equal(page.data.can_confirm, true);
});

test('outbound list refreshes whenever the task screen returns', () => {
  const session = {
    user: { username: 'outbound' },
    warehouseId: 1,
  };
  installMock('utils/session.js', {
    requireSession: () => session,
  });
  installMock('utils/request.js', {
    request: async () => ({ orders: [] }),
    upload: async () => ({}),
  });
  global.wx = {};

  const page = instantiate(loadPage('pages/outbound/index.js'));
  let loads = 0;
  page.loadOrders = async () => {
    loads += 1;
  };

  page.onLoad();
  assert.equal(loads, 0);
  page.onShow();
  assert.equal(loads, 1);
  page.onShow();
  assert.equal(loads, 2);
});
