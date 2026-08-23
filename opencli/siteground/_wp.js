import { CommandExecutionError, EmptyResultError } from './_runtime.js';


const PORTAL_ORIGIN = 'https://my.siteground.com';
const PORTAL_PATH = '/websites/list';
const UAPI_ORIGIN = 'https://uapi.siteground.com';
const SITES_LIST_PATH = '/v1/sites/list';
const AUTOLOGIN_PREFIX = '/v1/auth/wordpress/autologin';
const AUTOLOGIN_FILE = /^\/wp_auto_login_[a-f0-9]{16,64}\.php$/;
const WP_ADMIN_PATH = /^\/wp-admin\/?/;

const SITE_ID = /^[A-Za-z0-9_-]{8,128}$/;
const APP_ID = /^[0-9]{1,6}$/;

const SESSION_ERRORS = Object.freeze({
  no_session: 'SiteGround is not signed in in the configured OpenCLI Chrome profile.',
  unauthorized: 'The SiteGround portal session is expired; open my.siteground.com and sign in again.',
  malformed: 'SiteGround returned an unexpected website inventory shape.',
});


export function normalizeSiteId(value) {
  const normalized = String(value ?? '').trim();
  if (!SITE_ID.test(normalized)) {
    throw new TypeError('An exact non-secret SiteGround site id is required.');
  }
  return normalized;
}


export function normalizeAppId(value) {
  const normalized = String(value ?? '').trim();
  if (!APP_ID.test(normalized)) {
    throw new TypeError('An exact numeric SiteGround WordPress application id is required.');
  }
  return normalized;
}


function unwrap(payload, label) {
  const value = payload && typeof payload === 'object' && !Array.isArray(payload) && 'data' in payload
    ? payload.data
    : payload;
  if (!value || typeof value !== 'object') {
    throw new CommandExecutionError(`${label} returned malformed visible browser data.`);
  }
  if (typeof value.error === 'string') {
    throw new CommandExecutionError(SESSION_ERRORS[value.error] ?? `${label} failed: ${value.error}.`);
  }
  return value;
}


function adminHostOf(adminUrl) {
  try {
    return new URL(String(adminUrl)).hostname.toLowerCase();
  } catch {
    return '';
  }
}


/**
 * One row per installed WordPress application. Three provider behaviours make
 * a positional guess wrong here, and all three are observed on this account:
 *  - the application id is not always 1 (one live site is numbered 3);
 *  - a site with a staging copy has more than one application;
 *  - every application reports the *site's* domain, so a staging application is
 *    labelled `example.com` while its admin URL is `staging2.example.com`.
 * `admin_host` is therefore carried separately, because that — not `domain` —
 * is the host a login actually lands on.
 */
export function toAppRows(websites, label = 'SiteGround WordPress applications') {
  if (!Array.isArray(websites)) {
    throw new CommandExecutionError(`${label} returned malformed visible browser data.`);
  }
  const rows = websites.flatMap((site) => {
    const domain = String(site?.domain ?? '').trim();
    const siteId = String(site?.site_id ?? '').trim();
    const adminUrls = site?.admin_urls;
    if (!domain || !SITE_ID.test(siteId) || !adminUrls || typeof adminUrls !== 'object') return [];
    return Object.entries(adminUrls)
      .filter(([appId, adminUrl]) => APP_ID.test(String(appId)) && typeof adminUrl === 'string' && adminUrl)
      .map(([appId, adminUrl]) => ({
        domain,
        admin_host: adminHostOf(adminUrl),
        site_id: siteId,
        app_id: String(appId),
        cms: String(site?.cms ?? ''),
        admin_url: String(adminUrl),
        status: String(site?.status ?? ''),
      }))
      .filter((row) => row.admin_host);
  });
  if (!rows.length) throw new EmptyResultError(label);
  return rows;
}


/**
 * Refuse an ambiguous target instead of guessing. A site with production and
 * staging applications must name which one, because logging into the wrong copy
 * looks identical to logging into the right one.
 */
export function selectAppRow(rows, siteId, requestedAppId = undefined) {
  const normalizedSiteId = normalizeSiteId(siteId);
  const candidates = rows.filter((row) => row.site_id === normalizedSiteId);
  if (!candidates.length) {
    throw new CommandExecutionError(
      `SiteGround has no WordPress application for site id ${normalizedSiteId}. Run "opencli siteground wp-apps" for the exact ids.`,
    );
  }
  if (requestedAppId === undefined || requestedAppId === null || requestedAppId === '') {
    if (candidates.length > 1) {
      const available = candidates.map((row) => row.app_id).join(', ');
      throw new CommandExecutionError(
        `Site ${normalizedSiteId} has more than one WordPress application (${available}). Pass --app with the exact id.`,
      );
    }
    return candidates[0];
  }
  const normalizedAppId = normalizeAppId(requestedAppId);
  const match = candidates.find((row) => row.app_id === normalizedAppId);
  if (!match) {
    const available = candidates.map((row) => row.app_id).join(', ');
    throw new CommandExecutionError(
      `Site ${normalizedSiteId} has no WordPress application ${normalizedAppId} (available: ${available}).`,
    );
  }
  return match;
}


function hostOf(value, label) {
  let parsed;
  try {
    parsed = new URL(String(value ?? ''));
  } catch {
    throw new CommandExecutionError(`${label} returned an unparseable URL.`);
  }
  return parsed;
}


/**
 * The minted autologin URL is a single-use administrator credential. Verify it
 * points at the exact site that was asked for before the browser follows it, so
 * a drifted or hostile response cannot log this profile into another host.
 */
export function assertAutologinTarget(autologinUrl, expectedAdminUrl) {
  const minted = hostOf(autologinUrl, 'SiteGround WordPress autologin');
  const expected = hostOf(expectedAdminUrl, 'SiteGround WordPress application');
  if (minted.protocol !== 'https:') {
    throw new CommandExecutionError('SiteGround returned a non-HTTPS WordPress autologin URL.');
  }
  if (minted.username || minted.password || minted.search || minted.hash) {
    throw new CommandExecutionError('SiteGround returned a WordPress autologin URL with unexpected parts.');
  }
  if (minted.hostname.toLowerCase() !== expected.hostname.toLowerCase()) {
    throw new CommandExecutionError('SiteGround returned a WordPress autologin URL for a different host.');
  }
  if (!AUTOLOGIN_FILE.test(minted.pathname)) {
    throw new CommandExecutionError('SiteGround returned an unrecognized WordPress autologin path.');
  }
  return minted.hostname.toLowerCase();
}


export function assertLandedInAdmin(landedUrl, expectedHostname, label = 'SiteGround WordPress autologin') {
  const landed = hostOf(landedUrl, label);
  if (landed.protocol !== 'https:' || landed.hostname.toLowerCase() !== expectedHostname) {
    throw new CommandExecutionError(`${label} did not land on the requested site.`);
  }
  if (!WP_ADMIN_PATH.test(landed.pathname)) {
    throw new CommandExecutionError(`${label} did not land in wp-admin.`);
  }
  return `${landed.origin}${landed.pathname}`;
}


export async function gotoPortalRoot(page, label) {
  if (!page || typeof page.goto !== 'function' || typeof page.evaluate !== 'function') {
    throw new CommandExecutionError(`${label} requires the OpenCLI Browser Bridge.`);
  }
  await page.goto(`${PORTAL_ORIGIN}${PORTAL_PATH}`, { waitUntil: 'domcontentloaded' });
  if (typeof page.wait === 'function') {
    await page.wait({ selector: 'table tbody tr', timeout: 20 });
  }
}


/**
 * The portal access token lives in the signed-in profile's localStorage and is
 * refreshed by SiteGround's own app. Every authenticated request is issued
 * inside the page, so the token is never returned to the CLI process, printed,
 * or written to a receipt.
 */
export async function fetchWordPressApps(page, label = 'SiteGround WordPress applications') {
  const payload = unwrap(await page.evaluate(`(async () => {
    let token = null;
    try { token = JSON.parse(localStorage.getItem('ua_session')).session.token; } catch (error) { token = null; }
    if (!token) return { error: 'no_session' };
    const response = await fetch('${UAPI_ORIGIN}${SITES_LIST_PATH}', {
      headers: { Accept: 'application/json', Authorization: 'Bearer ' + token },
    });
    if (response.status === 401 || response.status === 403) return { error: 'unauthorized' };
    if (!response.ok) return { error: 'http_' + response.status };
    const body = await response.json().catch(() => null);
    const websites = body && body.data && Array.isArray(body.data.websites) ? body.data.websites : null;
    if (!websites) return { error: 'malformed' };
    return { websites: websites.map((site) => ({
      domain: String(site.domain || ''),
      site_id: String(site.site_id || site.id || ''),
      cms: String(site.cms || ''),
      status: String(site.status || ''),
      admin_urls: (site.cms_admin_url && typeof site.cms_admin_url === 'object') ? site.cms_admin_url : {},
    })) };
  })()`), label);
  return toAppRows(payload.websites, label);
}


const ADMIN_SETTLE_ATTEMPTS = 30;
const ADMIN_SETTLE_INTERVAL_MS = 1000;

const READBACK = `(() => ({
  url: location.href,
  title: document.title || '',
  is_admin: document.body ? document.body.classList.contains('wp-admin') : false
}))()`;


async function followInNewTab(page, url, label) {
  if (typeof page.newTab !== 'function' || typeof page.setActivePage !== 'function') {
    await page.goto(url, { waitUntil: 'load' });
    return unwrap(await page.evaluate(READBACK), label);
  }
  const previous = typeof page.getActivePage === 'function' ? page.getActivePage() : undefined;
  const target = await page.newTab(url);
  if (!target) {
    page.setActivePage(previous);
    throw new Error('the browser did not open a tab for the login');
  }
  page.setActivePage(target);

  // The autologin file redirects into wp-admin, which on shared hosting can
  // take a while to render. Poll for the signed-in document instead of trusting
  // a single early read that would report the redirect hop as a failure.
  let last = null;
  for (let attempt = 0; attempt < ADMIN_SETTLE_ATTEMPTS; attempt += 1) {
    last = unwrap(await page.evaluate(READBACK), label);
    if (last.is_admin && WP_ADMIN_PATH.test(pathnameOf(last.url))) return last;
    await new Promise((resolve) => setTimeout(resolve, ADMIN_SETTLE_INTERVAL_MS));
  }
  return last;
}


function pathnameOf(value) {
  try {
    return new URL(String(value ?? '')).pathname;
  } catch {
    return '';
  }
}


/**
 * Mint a single-use autologin URL and follow it inside the browser. The URL is
 * a working administrator credential, so it is consumed in the same step that
 * created it and never crosses back into the CLI process.
 */
export async function openWordPressAdmin(page, row, label = 'SiteGround WordPress autologin') {
  const siteId = normalizeSiteId(row.site_id);
  const appId = normalizeAppId(row.app_id);
  const minted = unwrap(await page.evaluate(`(async () => {
    let token = null;
    try { token = JSON.parse(localStorage.getItem('ua_session')).session.token; } catch (error) { token = null; }
    if (!token) return { error: 'no_session' };
    const response = await fetch('${UAPI_ORIGIN}${AUTOLOGIN_PREFIX}/${siteId}/${appId}', {
      headers: { Accept: 'application/json', Authorization: 'Bearer ' + token },
    });
    if (response.status === 401 || response.status === 403) return { error: 'unauthorized' };
    if (!response.ok) return { error: 'http_' + response.status };
    const body = await response.json().catch(() => null);
    const url = body && body.data ? body.data.autologin_url : null;
    if (typeof url !== 'string' || !url) return { error: 'malformed' };
    return { autologin_url: url };
  })()`), label);

  const expectedHostname = assertAutologinTarget(minted.autologin_url, row.admin_url);

  // From here the credential exists on the server. Anything that fails before
  // the readback leaves an unconsumed autologin file behind, so say so instead
  // of reporting a clean failure.
  //
  // The login is followed in a NEW tab rather than by navigating this one. The
  // adapter session is bound to siteground.com, so navigating it to the customer
  // domain detaches the target mid-flight ("Detached while handling command").
  // A new tab also leaves the portal tab where it was and leaves wp-admin open
  // for the operator instead of stealing the window they were using.
  let landed;
  try {
    landed = await followInNewTab(page, minted.autologin_url, label);
  } catch (error) {
    throw new CommandExecutionError(
      `${label} minted a single-use login for ${expectedHostname} but could not follow it; the unused link expires on its own. Cause: ${error.message}`,
    );
  }

  const wpAdminUrl = assertLandedInAdmin(landed.url, expectedHostname, label);
  if (!landed.is_admin) {
    throw new CommandExecutionError(`${label} reached ${wpAdminUrl} without a signed-in wp-admin document.`);
  }
  return {
    domain: row.domain,
    admin_host: expectedHostname,
    site_id: siteId,
    app_id: appId,
    wp_admin_url: wpAdminUrl,
    page_title: String(landed.title || ''),
    logged_in: true,
  };
}
