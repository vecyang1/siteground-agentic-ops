import { ArgumentError, cli, Strategy } from './_runtime.js';
import { gotoPortal, readTable } from './_ui.js';


export const paymentHistoryCommand = cli({
  site: 'siteground',
  name: 'payment-history',
  access: 'read',
  description: 'Read visible SiteGround payment-history rows without downloading invoices.',
  domain: 'siteground.com',
  strategy: Strategy.UI,
  browser: true,
  siteSession: 'persistent',
  navigateBefore: false,
  args: [{ name: 'limit', type: 'int', required: false, default: 25, help: 'Maximum rows, from 1 to 100.' }],
  columns: ['date', 'transaction', 'amount', 'documents'],
  func: async (page, kwargs) => {
    if (!Number.isInteger(kwargs.limit) || kwargs.limit < 1 || kwargs.limit > 100) {
      throw new ArgumentError('--limit must be an integer from 1 to 100.');
    }
    await gotoPortal(page, 'paymentHistory', 'SiteGround payment history');
    return (await readTable(page, 'SiteGround payment history')).slice(0, kwargs.limit).map((row) => ({
      date: row.date || '',
      transaction: row.transaction || '',
      amount: row.amount || '',
      documents: row.documents || '',
    }));
  },
});
