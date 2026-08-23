import { cli, Strategy } from './_runtime.js';
import { gotoPortal, readHostingCards } from './_ui.js';


export const hostingCommand = cli({
  site: 'siteground',
  name: 'hosting',
  access: 'read',
  description: 'Read visible SiteGround hosting plan names, types, and expiry dates.',
  domain: 'siteground.com',
  strategy: Strategy.UI,
  browser: true,
  siteSession: 'persistent',
  navigateBefore: false,
  args: [],
  columns: ['plan', 'type', 'expires_on'],
  func: async (page) => {
    await gotoPortal(page, 'hosting', 'SiteGround hosting plans');
    return readHostingCards(page);
  },
});
