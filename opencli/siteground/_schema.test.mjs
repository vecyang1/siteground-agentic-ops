import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizePlanId,
  parsePaymentMethodLabels,
  parseStatisticsText,
} from './_schema.js';


test('normalizePlanId accepts exact opaque ids and rejects path fragments', () => {
  assert.equal(normalizePlanId('EXAMPLEPLANID005'), 'EXAMPLEPLANID005');
  assert.throws(() => normalizePlanId('../billing/renew'), /provider plan id/i);
});


test('parseStatisticsText returns numeric web-space and inode rows', () => {
  const rows = parseStatisticsText(`
    Web Space 100 GB 86.4% 13.64 GB Used, 86.36 GB Free
    Inodes 600,000 41.9% 495,048 Used, 104,952 Free
  `);

  assert.deepEqual(rows, [
    { metric: 'web_space_limit', value: 100, unit: 'GB' },
    { metric: 'web_space_used', value: 13.64, unit: 'GB' },
    { metric: 'web_space_free', value: 86.36, unit: 'GB' },
    { metric: 'web_space_used_percent', value: 13.64, unit: 'percent' },
    { metric: 'inodes_limit', value: 600000, unit: 'count' },
    { metric: 'inodes_used', value: 495048, unit: 'count' },
    { metric: 'inodes_free', value: 104952, unit: 'count' },
    { metric: 'inodes_used_percent', value: 82.508, unit: 'percent' },
  ]);
});


test('parsePaymentMethodLabels excludes card endings from normal output', () => {
  const rows = parsePaymentMethodLabels([
    'Payment method Primary card PayPal ending at expires at',
    'Payment method Alternative card Visa ending at 9048 expires at 10/31',
  ]);

  assert.deepEqual(rows, [
    { role: 'Primary', method: 'card', brand: 'PayPal', expires: '' },
    { role: 'Alternative', method: 'card', brand: 'Visa', expires: '10/31' },
  ]);
  assert.equal(JSON.stringify(rows).includes('9048'), false);
});
