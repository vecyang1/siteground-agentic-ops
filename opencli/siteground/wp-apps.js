import { cli, Strategy } from './_runtime.js';
import { fetchWordPressApps, gotoPortalRoot } from './_wp.js';


export const wpAppsCommand = cli({
  site: 'siteground',
  name: 'wp-apps',
  access: 'read',
  description: 'Read the exact SiteGround site and WordPress application ids needed to open wp-admin.',
  domain: 'siteground.com',
  strategy: Strategy.UI,
  browser: true,
  siteSession: 'persistent',
  navigateBefore: false,
  args: [],
  columns: ['domain', 'admin_host', 'site_id', 'app_id', 'cms', 'admin_url', 'status'],
  func: async (page) => {
    await gotoPortalRoot(page, 'SiteGround WordPress applications');
    return fetchWordPressApps(page);
  },
});
