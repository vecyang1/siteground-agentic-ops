import { cli, Strategy } from './_runtime.js';
import { gotoPortal, readRenewalCards } from './_ui.js';


export const renewalsCommand = cli({
  site: 'siteground',
  name: 'renewals',
  access: 'read',
  description: 'Read visible SiteGround renewal cards; this command never changes renewal selection.',
  domain: 'siteground.com',
  strategy: Strategy.UI,
  browser: true,
  siteSession: 'persistent',
  navigateBefore: false,
  args: [],
  columns: ['service', 'expiry', 'term', 'rate', 'displayed_total', 'selected_for_manual_renewal'],
  func: async (page) => {
    await gotoPortal(page, 'renewals', 'SiteGround renewals');
    return readRenewalCards(page, 'SiteGround renewals');
  },
});
