import { cli, Strategy } from './_runtime.js';
import { gotoPortal, readTable } from './_ui.js';


export const websitesCommand = cli({
  site: 'siteground',
  name: 'websites',
  access: 'read',
  description: 'Read the visible SiteGround website inventory.',
  domain: 'siteground.com',
  strategy: Strategy.UI,
  browser: true,
  siteSession: 'persistent',
  navigateBefore: false,
  args: [],
  columns: ['domain', 'plan', 'site_created', 'status'],
  func: async (page) => {
    await gotoPortal(page, 'websites', 'SiteGround websites');
    return (await readTable(page, 'SiteGround websites')).map((row) => ({
      domain: row.domain || '',
      plan: row.plan || '',
      site_created: row.site_created || '',
      status: row.status || '',
    }));
  },
});
