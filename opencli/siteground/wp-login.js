import { cli, Strategy } from './_runtime.js';
import { fetchWordPressApps, gotoPortalRoot, openWordPressAdmin, selectAppRow } from './_wp.js';


export const wpLoginCommand = cli({
  site: 'siteground',
  name: 'wp-login',
  access: 'write',
  description: 'Open one exact SiteGround WordPress application in wp-admin using a single-use provider autologin.',
  domain: 'siteground.com',
  strategy: Strategy.UI,
  browser: true,
  siteSession: 'persistent',
  navigateBefore: false,
  args: [
    { name: 'site-id', required: true, help: 'Exact non-secret SiteGround site id from "siteground wp-apps".' },
    { name: 'app', required: false, help: 'Exact WordPress application id; required when the site has more than one.' },
  ],
  columns: ['domain', 'admin_host', 'site_id', 'app_id', 'wp_admin_url', 'page_title', 'logged_in'],
  func: async (page, kwargs) => {
    const label = 'SiteGround WordPress autologin';
    await gotoPortalRoot(page, label);
    const rows = await fetchWordPressApps(page, label);
    const row = selectAppRow(rows, kwargs['site-id'], kwargs.app);
    return [await openWordPressAdmin(page, row, label)];
  },
});
