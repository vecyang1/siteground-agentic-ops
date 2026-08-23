import { cli, Strategy } from './_runtime.js';
import { gotoPortal, readTable } from './_ui.js';


export const planSitesCommand = cli({
  site: 'siteground',
  name: 'plan-sites',
  access: 'read',
  description: 'Read visible member sites and regions for one exact SiteGround hosting plan.',
  domain: 'siteground.com',
  strategy: Strategy.UI,
  browser: true,
  siteSession: 'persistent',
  navigateBefore: false,
  args: [{ name: 'plan-id', required: true, help: 'Exact non-secret SiteGround hosting plan id.' }],
  columns: ['name', 'status', 'data_center'],
  func: async (page, kwargs) => {
    await gotoPortal(page, 'planSites', 'SiteGround plan sites', kwargs['plan-id']);
    return (await readTable(page, 'SiteGround plan sites')).map((row) => ({
      name: row.name || '',
      status: row.status || '',
      data_center: row.data_center || '',
    }));
  },
});
