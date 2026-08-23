import assert from 'node:assert/strict';
import test from 'node:test';

import { gotoPortal, parseRenewalCards, readRenewalCards } from './_ui.js';


test('gotoPortal waits for the SiteGround websites table to finish rendering', async () => {
  const waits = [];
  const page = {
    goto: async () => {},
    wait: async (condition) => waits.push(condition),
    evaluate: async () => ({
      title: 'My SiteGround Account > Websites',
      heading: 'My Websites',
      text: '',
      table_rows: 8,
      hosting_plans: 0,
      payment_methods: 0,
      statistics_ready: false,
    }),
  };

  await gotoPortal(page, 'websites', 'SiteGround websites');

  assert.deepEqual(waits, [{ selector: 'table tbody tr', timeout: 20 }]);
});


test('gotoPortal waits for visible renewal cards instead of a table', async () => {
  const waits = [];
  const page = {
    goto: async () => {},
    wait: async (condition) => waits.push(condition),
    evaluate: async () => ({
      title: 'My SiteGround Account > Renewals',
      heading: 'Renew your services',
      text: 'Renewal options',
      table_rows: 0,
      renewal_cards: 2,
      hosting_plans: 0,
      payment_methods: 0,
      statistics_ready: false,
    }),
  };

  await gotoPortal(page, 'renewals', 'SiteGround renewals');

  assert.deepEqual(waits, [{ selector: 'label[role="checkbox"][aria-checked]', timeout: 20 }]);
});


// Column texts captured verbatim from https://my.siteground.com/billing/renew on
// 2026-08-23. SiteGround lays each service out as one `div.service` row of three
// columns: identity + expiry, offered term + rate, displayed total. Every card was
// unchecked, which is this account's steady state.
const CAPTURED_RENEWAL_COLUMNS = [
  {
    service_column: 'Hosting Plan 6 251128 GoGeek Hosting Expires Nov 28, 2026',
    offer_column: '12 months - $44.99/mo (Save 10%)',
    total_column: '$539.88 EX. VAT',
    checkbox_checked: false,
  },
  {
    service_column: 'Premium Backup (12 months) example-main.com Expires Dec 2, 2026',
    offer_column: '12 months - $4.99/mo',
    total_column: '$59.88 EX. VAT',
    checkbox_checked: false,
  },
  {
    service_column: 'Hosting Plan 7 251130 GrowBig Hosting Expires Nov 30, 2026',
    offer_column: '12 months - $29.99/mo (Save 14%)',
    total_column: '$359.88 EX. VAT',
    checkbox_checked: false,
  },
];


test('parseRenewalCards reads the captured SiteGround columns into offer fields', () => {
  assert.deepEqual(parseRenewalCards(CAPTURED_RENEWAL_COLUMNS), [
    {
      service: 'Hosting Plan 6 251128 GoGeek Hosting',
      expiry: 'Nov 28, 2026',
      term: '12 months',
      rate: '$44.99/mo (Save 10%)',
      displayed_total: '$539.88 EX. VAT',
      selected_for_manual_renewal: false,
    },
    {
      service: 'Premium Backup (12 months) example-main.com',
      expiry: 'Dec 2, 2026',
      term: '12 months',
      rate: '$4.99/mo',
      displayed_total: '$59.88 EX. VAT',
      selected_for_manual_renewal: false,
    },
    {
      service: 'Hosting Plan 7 251130 GrowBig Hosting',
      expiry: 'Nov 30, 2026',
      term: '12 months',
      rate: '$29.99/mo (Save 14%)',
      displayed_total: '$359.88 EX. VAT',
      selected_for_manual_renewal: false,
    },
  ]);
});


test('an all-unselected renewals page is a complete read, not an incomplete one', () => {
  const rows = parseRenewalCards(CAPTURED_RENEWAL_COLUMNS);

  assert.equal(rows.length, 3);
  assert.equal(rows.every((row) => row.selected_for_manual_renewal === false), true);
  assert.equal(rows.every((row) => row.displayed_total !== ''), true);
});


test('a ticked card is the only thing that reports selected_for_manual_renewal', () => {
  const rows = parseRenewalCards(CAPTURED_RENEWAL_COLUMNS.map((card, index) => (
    index === 1 ? { ...card, checkbox_checked: true } : card
  )));

  assert.deepEqual(rows.map((row) => row.selected_for_manual_renewal), [false, true, false]);
});


test('parseRenewalCards refuses incomplete visible card data', () => {
  assert.throws(() => parseRenewalCards([{
    service_column: 'Hosting Plan 6 251128 GoGeek Hosting Expires Nov 28, 2026',
    offer_column: '',
    total_column: '',
    checkbox_checked: false,
  }]), /did not expose complete visible card data/i);
});


test('the refusal names the missing fields and denies being a renewal setting', () => {
  assert.throws(() => parseRenewalCards([{
    service_column: 'Hosting Plan 6 251128 GoGeek Hosting Expires Nov 28, 2026',
    offer_column: '',
    total_column: '$539.88 EX. VAT',
    checkbox_checked: false,
  }]), (error) => {
    assert.match(error.message, /Hosting Plan 6 251128 GoGeek Hosting/);
    assert.match(error.message, /term/);
    assert.match(error.message, /rate/);
    assert.doesNotMatch(error.message, /displayed_total/);
    assert.match(error.message, /read failure, not a renewal setting/i);
    return true;
  });
});


test('an unreadable checkbox is refused, never reported as "not renewing"', () => {
  assert.throws(() => parseRenewalCards([{ ...CAPTURED_RENEWAL_COLUMNS[0], checkbox_checked: null }]), (error) => {
    assert.match(error.message, /selected_for_manual_renewal/);
    return true;
  });
  assert.throws(() => parseRenewalCards([{ ...CAPTURED_RENEWAL_COLUMNS[0], checkbox_checked: 'false' }]),
    /did not expose complete visible card data/i);
});


test('parseRenewalCards refuses a page that rendered no cards', () => {
  assert.throws(() => parseRenewalCards([]), /returned no data/i);
});


test('readRenewalCards reads the page once and never ticks a renewal box', async () => {
  const evaluated = [];
  const page = {
    evaluate: async (script) => {
      evaluated.push(script);
      return { data: CAPTURED_RENEWAL_COLUMNS };
    },
  };

  const rows = await readRenewalCards(page, 'SiteGround renewals');

  assert.equal(rows.length, 3);
  assert.equal(rows[0].service, 'Hosting Plan 6 251128 GoGeek Hosting');
  assert.equal(rows[0].displayed_total, '$539.88 EX. VAT');
  assert.equal(rows.some((row) => Object.hasOwn(row, 'auto_renew')), false);
  assert.equal(rows.every((row) => row.selected_for_manual_renewal === false), true);
  assert.equal(evaluated.length, 1);
  assert.equal(/\.click\(|\.fill\(|\.type\(|\.check\(|\.uncheck\(/.test(evaluated[0]), false);
});
