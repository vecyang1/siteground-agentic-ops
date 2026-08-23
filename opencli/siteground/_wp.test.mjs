import assert from 'node:assert/strict';
import test from 'node:test';

import {
  assertAutologinTarget,
  assertLandedInAdmin,
  normalizeAppId,
  normalizeSiteId,
  openWordPressAdmin,
  selectAppRow,
  toAppRows,
} from './_wp.js';


const SITE_ID = 'EXAMPLESITEID003';
const OTHER_SITE_ID = 'EXAMPLESITEID001';
const AUTOLOGIN = `https://example-shop.com/wp_auto_login_${'a1b2c3d4'.repeat(4)}.php`;


function appRow(overrides = {}) {
  return {
    domain: 'example-shop.com',
    admin_host: 'example-shop.com',
    site_id: SITE_ID,
    app_id: '1',
    cms: 'woocommerce',
    admin_url: 'https://example-shop.com/wp-admin',
    status: 'active',
    ...overrides,
  };
}


test('toAppRows emits one row per installed application, keyed on the observed id', () => {
  const rows = toAppRows([
    {
      domain: 'example-main.com',
      site_id: SITE_ID,
      cms: 'wordpress',
      status: 'active',
      admin_urls: { 1: 'http://example-main.com/wp-admin', 2: 'http://staging2.example-main.com/wp-admin' },
    },
    {
      domain: 'example-lang.com',
      site_id: OTHER_SITE_ID,
      cms: 'wordpress',
      status: 'active',
      admin_urls: { 3: 'https://example-lang.com/wp-admin' },
    },
  ]);

  assert.deepEqual(rows.map((row) => [row.domain, row.admin_host, row.app_id]), [
    ['example-main.com', 'example-main.com', '1'],
    ['example-main.com', 'staging2.example-main.com', '2'],
    ['example-lang.com', 'example-lang.com', '3'],
  ]);
});


test('toAppRows keeps a site whose only application id is not 1', () => {
  const rows = toAppRows([
    { domain: 'example-lang.com', site_id: SITE_ID, cms: 'wordpress', status: 'active', admin_urls: { 3: 'https://example-lang.com/wp-admin' } },
  ]);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].app_id, '3');
});


test('toAppRows carries the staging admin host even when the site domain is shared', () => {
  const rows = toAppRows([
    {
      domain: 'example-main.com',
      site_id: SITE_ID,
      cms: 'wordpress',
      status: 'active',
      admin_urls: { 2: 'http://staging2.example-main.com/wp-admin' },
    },
  ]);

  // The provider labels a staging application with the production domain, so
  // `domain` alone cannot tell the two copies apart.
  assert.equal(rows[0].domain, 'example-main.com');
  assert.equal(rows[0].admin_host, 'staging2.example-main.com');
});


test('toAppRows refuses an inventory with no readable application', () => {
  assert.throws(
    () => toAppRows([{ domain: 'example.com', site_id: SITE_ID, admin_urls: {} }]),
    /SiteGround WordPress applications/,
  );
});


test('selectAppRow returns the only application without requiring an explicit id', () => {
  const row = selectAppRow([appRow()], SITE_ID);
  assert.equal(row.app_id, '1');
});


test('selectAppRow refuses to guess between production and staging', () => {
  const rows = [appRow(), appRow({ app_id: '2', admin_host: 'staging2.example-shop.com', admin_url: 'https://staging2.example-shop.com/wp-admin' })];
  assert.throws(() => selectAppRow(rows, SITE_ID), /more than one WordPress application \(1, 2\)/);
});


test('selectAppRow refuses an application id the site does not have', () => {
  assert.throws(() => selectAppRow([appRow()], SITE_ID, '7'), /no WordPress application 7 \(available: 1\)/);
});


test('selectAppRow refuses an unknown site id', () => {
  assert.throws(() => selectAppRow([appRow()], OTHER_SITE_ID), /no WordPress application for site id/);
});


test('normalizeSiteId and normalizeAppId reject values that could reshape the request path', () => {
  for (const bad of ['', '../../v1', `${SITE_ID}/9`, 'short']) {
    assert.throws(() => normalizeSiteId(bad), TypeError);
  }
  for (const bad of ['', '1/2', 'one', '-1']) {
    assert.throws(() => normalizeAppId(bad), TypeError);
  }
});


test('assertAutologinTarget accepts the provider single-use login for the requested host', () => {
  assert.equal(assertAutologinTarget(AUTOLOGIN, 'https://example-shop.com/wp-admin'), 'example-shop.com');
});


test('assertAutologinTarget refuses a login minted for a different host', () => {
  assert.throws(
    () => assertAutologinTarget(AUTOLOGIN, 'https://example-studio.com/wp-admin'),
    /different host/,
  );
});


test('assertAutologinTarget refuses a non-HTTPS, decorated, or unrecognized login link', () => {
  assert.throws(() => assertAutologinTarget(AUTOLOGIN.replace('https:', 'http:'), 'http://example-shop.com/wp-admin'), /non-HTTPS/);
  assert.throws(() => assertAutologinTarget(`${AUTOLOGIN}?next=//evil.example`, 'https://example-shop.com/wp-admin'), /unexpected parts/);
  assert.throws(() => assertAutologinTarget('https://example-shop.com/wp-login.php', 'https://example-shop.com/wp-admin'), /unrecognized/);
});


test('assertLandedInAdmin requires the readback to be wp-admin on the requested host', () => {
  assert.equal(
    assertLandedInAdmin('https://example-shop.com/wp-admin/?sg_auto=1', 'example-shop.com'),
    'https://example-shop.com/wp-admin/',
  );
  assert.throws(() => assertLandedInAdmin('https://example-shop.com/wp-login.php', 'example-shop.com'), /did not land in wp-admin/);
  assert.throws(() => assertLandedInAdmin('https://evil.example/wp-admin/', 'example-shop.com'), /did not land on the requested site/);
});


function stubPage(steps) {
  return {
    goto: async (url) => steps.push(['goto', url]),
    evaluate: async (code) => {
      steps.push(['evaluate']);
      if (code.includes('autologin')) return { data: { autologin_url: AUTOLOGIN } };
      return { data: { url: 'https://example-shop.com/wp-admin/?sg_auto=1', title: 'Dashboard ‹ ExampleShop — WordPress', is_admin: true } };
    },
  };
}


test('openWordPressAdmin consumes the login in the browser and never returns the credential', async () => {
  const steps = [];
  const result = await openWordPressAdmin(stubPage(steps), appRow());

  assert.deepEqual(result, {
    domain: 'example-shop.com',
    admin_host: 'example-shop.com',
    site_id: SITE_ID,
    app_id: '1',
    wp_admin_url: 'https://example-shop.com/wp-admin/',
    page_title: 'Dashboard ‹ ExampleShop — WordPress',
    logged_in: true,
  });
  assert.equal(steps.filter(([kind]) => kind === 'goto').length, 1);
  assert.ok(!JSON.stringify(result).includes('wp_auto_login_'));
});


test('openWordPressAdmin reports an unconsumed single-use login when the follow-through fails', async () => {
  const page = {
    goto: async () => { throw new Error('browser connection dropped'); },
    evaluate: async () => ({ data: { autologin_url: AUTOLOGIN } }),
  };

  await assert.rejects(
    () => openWordPressAdmin(page, appRow()),
    /minted a single-use login for example-shop\.com but could not follow it/,
  );
});


test('openWordPressAdmin refuses a page that is not a signed-in wp-admin document', async () => {
  const page = {
    goto: async () => {},
    evaluate: async (code) => (code.includes('autologin')
      ? { data: { autologin_url: AUTOLOGIN } }
      : { data: { url: 'https://example-shop.com/wp-admin/', title: 'Log In', is_admin: false } }),
  };

  await assert.rejects(() => openWordPressAdmin(page, appRow()), /without a signed-in wp-admin document/);
});


test('openWordPressAdmin surfaces an expired portal session as a sign-in instruction', async () => {
  const page = {
    goto: async () => {},
    evaluate: async () => ({ data: { error: 'unauthorized' } }),
  };

  await assert.rejects(() => openWordPressAdmin(page, appRow()), /session is expired/);
});


test('openWordPressAdmin follows the login in a new tab so the portal tab is not detached', async () => {
  const events = [];
  let active = 'portal-target';
  const page = {
    goto: async (url) => events.push(['goto', url]),
    getActivePage: () => active,
    setActivePage: (target) => { active = target; events.push(['setActivePage', target]); },
    newTab: async (url) => { events.push(['newTab', url]); return 'wp-admin-target'; },
    evaluate: async (code) => (code.includes('autologin')
      ? { data: { autologin_url: AUTOLOGIN } }
      : { data: { url: 'https://example-shop.com/wp-admin/?sg_auto=1', title: 'Dashboard', is_admin: true } }),
  };

  const result = await openWordPressAdmin(page, appRow());

  assert.equal(result.logged_in, true);
  assert.equal(events.filter(([kind]) => kind === 'goto').length, 0, 'the bound portal tab must not be navigated');
  assert.deepEqual(events.filter(([kind]) => kind === 'newTab')[0], ['newTab', AUTOLOGIN]);
  assert.equal(active, 'wp-admin-target');
});


test('openWordPressAdmin restores the previous tab and reports a leak when no tab opens', async () => {
  let active = 'portal-target';
  const page = {
    goto: async () => {},
    getActivePage: () => active,
    setActivePage: (target) => { active = target; },
    newTab: async () => undefined,
    evaluate: async () => ({ data: { autologin_url: AUTOLOGIN } }),
  };

  await assert.rejects(
    () => openWordPressAdmin(page, appRow()),
    /minted a single-use login .* but could not follow it/,
  );
  assert.equal(active, 'portal-target');
});


test('openWordPressAdmin waits through the redirect hop instead of failing on the first read', async () => {
  let reads = 0;
  const page = {
    goto: async () => {},
    getActivePage: () => 'portal-target',
    setActivePage: () => {},
    newTab: async () => 'wp-admin-target',
    evaluate: async (code) => {
      if (code.includes('autologin')) return { data: { autologin_url: AUTOLOGIN } };
      reads += 1;
      // First read catches the autologin hop, not wp-admin yet.
      if (reads === 1) return { data: { url: AUTOLOGIN, title: '', is_admin: false } };
      return { data: { url: 'https://example-shop.com/wp-admin/', title: 'Dashboard', is_admin: true } };
    },
  };

  const result = await openWordPressAdmin(page, appRow());

  assert.equal(result.logged_in, true);
  assert.ok(reads >= 2, 'must re-read after the redirect hop');
});
