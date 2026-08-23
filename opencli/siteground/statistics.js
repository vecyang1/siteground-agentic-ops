import { cli, EmptyResultError, Strategy } from './_runtime.js';
import { parseStatisticsText } from './_schema.js';
import { gotoPortal, readBodyText } from './_ui.js';


export const statisticsCommand = cli({
  site: 'siteground',
  name: 'statistics',
  access: 'read',
  description: 'Read aggregate SiteGround web-space and inode statistics for one exact plan.',
  domain: 'siteground.com',
  strategy: Strategy.UI,
  browser: true,
  siteSession: 'persistent',
  navigateBefore: false,
  args: [{ name: 'plan-id', required: true, help: 'Exact non-secret SiteGround hosting plan id.' }],
  columns: ['metric', 'value', 'unit'],
  func: async (page, kwargs) => {
    await gotoPortal(page, 'statistics', 'SiteGround statistics', kwargs['plan-id']);
    const rows = parseStatisticsText(await readBodyText(page, 'SiteGround statistics'));
    if (!rows.length) throw new EmptyResultError('SiteGround statistics');
    return rows;
  },
});
