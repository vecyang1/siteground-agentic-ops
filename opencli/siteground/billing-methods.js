import { cli, EmptyResultError, Strategy } from './_runtime.js';
import { parsePaymentMethodLabels } from './_schema.js';
import { gotoPortal, readPaymentLabels } from './_ui.js';


export const billingMethodsCommand = cli({
  site: 'siteground',
  name: 'billing-methods',
  access: 'read',
  description: 'Read billing-method role, type, brand, and expiry without card endings or address data.',
  domain: 'siteground.com',
  strategy: Strategy.UI,
  browser: true,
  siteSession: 'persistent',
  navigateBefore: false,
  args: [],
  columns: ['role', 'method', 'brand', 'expires'],
  func: async (page) => {
    await gotoPortal(page, 'billingMethods', 'SiteGround billing methods');
    const rows = parsePaymentMethodLabels(await readPaymentLabels(page));
    if (!rows.length) throw new EmptyResultError('SiteGround billing methods');
    return rows;
  },
});
