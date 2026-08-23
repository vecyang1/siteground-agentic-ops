import { CommandExecutionError, EmptyResultError } from './_runtime.js';
import { normalizePlanId } from './_schema.js';


const ORIGIN = 'https://my.siteground.com';
const PATHS = Object.freeze({
  websites: '/websites/list',
  hosting: '/services/hosting',
  billingMethods: '/billing/details',
  paymentHistory: '/billing/payment-history',
  renewals: '/billing/renew',
});


function unwrap(payload, label) {
  const value = payload && typeof payload === 'object' && !Array.isArray(payload) && 'data' in payload
    ? payload.data
    : payload;
  if (!value || typeof value !== 'object') {
    throw new CommandExecutionError(`${label} returned malformed visible browser data.`);
  }
  return value;
}


function pageIsReady(pageName, state) {
  if (pageName === 'renewals') return state.renewal_cards > 0;
  if (pageName === 'websites' || pageName === 'planSites' || pageName === 'paymentHistory') {
    return state.table_rows > 0;
  }
  if (pageName === 'hosting') return state.hosting_plans > 0;
  if (pageName === 'billingMethods') return state.payment_methods > 0;
  if (pageName === 'statistics') return state.statistics_ready === true;
  return false;
}


async function waitForPortalContent(page, pageName) {
  if (typeof page.wait !== 'function') return;
  if (pageName === 'statistics') {
    await page.wait({ text: 'Web Space', timeout: 20 });
    return;
  }
  const selector = {
    websites: 'table tbody tr',
    hosting: 'h3',
    billingMethods: '[aria-label^="Payment method "]',
    paymentHistory: 'table tbody tr',
    planSites: 'table tbody tr',
    renewals: 'label[role="checkbox"][aria-checked]',
  }[pageName];
  if (!selector) throw new CommandExecutionError('SiteGround requested an unsupported visible-content wait.');
  await page.wait({ selector, timeout: 20 });
}


export async function gotoPortal(page, pageName, label, planId = undefined) {
  if (!page || typeof page.goto !== 'function' || typeof page.evaluate !== 'function') {
    throw new CommandExecutionError(`${label} requires the OpenCLI Browser Bridge.`);
  }
  let path;
  if (pageName === 'planSites' || pageName === 'statistics') {
    const normalized = normalizePlanId(planId);
    path = `/services/hosting/${normalized}${pageName === 'statistics' ? '/statistics' : ''}`;
  } else {
    path = PATHS[pageName];
  }
  if (!path) throw new CommandExecutionError(`${label} requested an unsupported SiteGround route.`);
  await page.goto(`${ORIGIN}${path}`, { waitUntil: 'domcontentloaded' });
  await waitForPortalContent(page, pageName);
  const state = unwrap(await page.evaluate(`(() => ({
    title: document.title || '',
    heading: document.querySelector('h1')?.innerText?.trim() || '',
    text: (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 600),
    table_rows: document.querySelectorAll('table tbody tr').length,
    renewal_cards: document.querySelectorAll('label[role="checkbox"][aria-checked]').length,
    hosting_plans: Array.from(document.querySelectorAll('h2,h3')).filter((node) => /^Hosting Plan\\s+/i.test((node.innerText || '').trim())).length,
    payment_methods: document.querySelectorAll('[aria-label^="Payment method "]').length,
    statistics_ready: /Web Space/i.test(document.body?.innerText || '') && /Inodes/i.test(document.body?.innerText || '')
  }))()`), label);
  if (/log in|sign in|forgot password/i.test(`${state.title} ${state.heading} ${state.text}`)) {
    throw new CommandExecutionError('SiteGround is not signed in in the configured OpenCLI Chrome profile.');
  }
  if (pageIsReady(pageName, state)) return;
  throw new EmptyResultError(`${label} did not finish loading visible data.`);
}


export async function readTable(page, label) {
  const rows = unwrap(await page.evaluate(`(() => {
    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
    const key = (value) => clean(value).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    const table = Array.from(document.querySelectorAll('table')).find((candidate) => candidate.querySelector('tbody tr'));
    if (!table) return [];
    const headers = Array.from(table.querySelectorAll('thead th')).map((cell) => key(cell.innerText));
    return Array.from(table.querySelectorAll('tbody tr')).map((row) => {
      const values = Array.from(row.querySelectorAll('td')).map((cell) => clean(cell.innerText));
      return Object.fromEntries(headers.map((header, index) => [header, values[index] || '']));
    });
  })()`), label);
  if (!Array.isArray(rows) || !rows.length) throw new EmptyResultError(label);
  return rows;
}


export async function readHostingCards(page) {
  const cards = unwrap(await page.evaluate(`(() => {
    const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
    const headings = Array.from(document.querySelectorAll('h2,h3')).filter((node) => /^Hosting Plan\\s+/i.test(clean(node.innerText)));
    return headings.map((heading) => {
      let card = heading.parentElement;
      while (card?.parentElement && (!/Expires\\s+/i.test(card.innerText || '') || clean(card.innerText).length < 25)) {
        card = card.parentElement;
      }
      const text = clean(card?.innerText || heading.innerText);
      return {
        plan: clean(heading.innerText),
        type: text.match(/\\b(GoGeek|GrowBig|StartUp)\\b/i)?.[1] || '',
        expires_on: text.match(/Expires\\s+([A-Z][a-z]{2}\\s+\\d{1,2},\\s+\\d{4})/i)?.[1] || '',
      };
    });
  })()`), 'SiteGround hosting plans');
  if (!Array.isArray(cards) || !cards.length) throw new EmptyResultError('SiteGround hosting plans');
  return cards;
}


export async function readBodyText(page, label) {
  const payload = unwrap(await page.evaluate(`(() => ({
    text: (document.querySelector('.ua-page-wrapper')?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 30000)
  }))()`), label);
  if (!payload.text) throw new EmptyResultError(label);
  return payload.text;
}


export async function readPaymentLabels(page) {
  const labels = unwrap(await page.evaluate(`(() => Array.from(
    document.querySelectorAll('[aria-label^="Payment method "]'),
    (node) => node.getAttribute('aria-label') || ''
  ))()`), 'SiteGround billing methods');
  if (!Array.isArray(labels) || !labels.length) throw new EmptyResultError('SiteGround billing methods');
  return labels;
}


// SiteGround renders each renewable service as one row of three visible columns:
// identity plus expiry (which holds the selection checkbox), the offered term and
// rate, and the displayed total. The columns are read verbatim in the page and
// parsed here, so the parsing is exercised by tests against captured page text
// rather than living inside an un-runnable evaluate() string.
const RENEWAL_FIELDS = Object.freeze(['service', 'expiry', 'term', 'rate', 'displayed_total']);
const EXPIRY_DATE = /Expires\s+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})/i;
const OFFER_SEPARATOR = /\s+[-\u2013\u2014]\s+/;
const OFFER_TERM = /^(\d+\s*(?:month|months|year|years))\b/i;


function cleanText(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}


function splitOffer(offerColumn) {
  const offer = cleanText(offerColumn);
  if (!offer) return { term: '', rate: '' };
  const parts = offer.split(OFFER_SEPARATOR);
  if (parts.length >= 2) {
    const term = cleanText(parts[0]);
    const rate = cleanText(parts.slice(1).join(' - '));
    if (term && rate) return { term, rate };
  }
  const matched = offer.match(OFFER_TERM);
  if (!matched) return { term: '', rate: offer };
  return { term: cleanText(matched[1]), rate: cleanText(offer.slice(matched[0].length)) };
}


// Selection is the one field this account reads to confirm that nothing is queued
// for renewal, so an unreadable checkbox stays null and is refused below. Reporting
// an unknown selection as `false` would claim "nothing renews" without having read it.
function readSelection(card) {
  if (card.checkbox_checked === true || card.checkbox_checked === false) return card.checkbox_checked;
  return null;
}


function normalizeRenewalCard(card) {
  if (!card || typeof card !== 'object') return null;
  const serviceColumn = cleanText(card.service_column);
  const expiry = serviceColumn.match(EXPIRY_DATE)?.[1] || '';
  const { term, rate } = splitOffer(card.offer_column);
  return {
    service: expiry ? cleanText(serviceColumn.replace(EXPIRY_DATE, '')) : serviceColumn,
    expiry: cleanText(expiry),
    term,
    rate,
    displayed_total: cleanText(card.total_column),
    selected_for_manual_renewal: readSelection(card),
  };
}


function missingFields(row) {
  const missing = RENEWAL_FIELDS.filter((field) => !row[field]);
  if (row.selected_for_manual_renewal === null) missing.push('selected_for_manual_renewal');
  return missing;
}


export function parseRenewalCards(cards, label = 'SiteGround renewals') {
  if (!Array.isArray(cards)) throw new CommandExecutionError(`${label} returned malformed visible browser data.`);
  const rows = cards.map(normalizeRenewalCard).filter(Boolean);
  if (!rows.length) throw new EmptyResultError(label);
  const incomplete = rows
    .map((row) => ({ row, missing: missingFields(row) }))
    .filter((entry) => entry.missing.length > 0);
  if (incomplete.length > 0) {
    const [first] = incomplete;
    throw new CommandExecutionError(
      `${label} did not expose complete visible card data for "${first.row.service || 'an unnamed service'}": `
      + `missing ${first.missing.join(', ')} (${incomplete.length} of ${rows.length} card(s) affected). `
      + 'This is a read failure, not a renewal setting; no renewal selection was read or changed.',
    );
  }
  return rows;
}


export async function readRenewalCards(page, label) {
  const cards = unwrap(await page.evaluate(`(() => {
    const clean = (value) => String(value ?? '').replace(/\\s+/g, ' ').trim();
    const MONEY = /[$€£¥]\\s?[\\d,]+(?:\\.\\d+)?/;
    const PER_PERIOD = /\\/\\s*(?:mo|month|yr|year)/i;
    const seen = new Set();

    return Array.from(document.querySelectorAll('label[role="checkbox"][aria-checked], input[type="checkbox"]')).flatMap((checkbox) => {
      let row = checkbox.parentElement;
      let hops = 0;
      while (row && hops < 8 && !(row.children.length >= 3 && MONEY.test(row.innerText || ''))) {
        row = row.parentElement;
        hops += 1;
      }
      if (!row || seen.has(row)) return [];
      seen.add(row);

      const columns = Array.from(row.children);
      const identity = columns.find((column) => column.contains(checkbox));
      const rest = columns.filter((column) => column !== identity);
      const total = rest.find((column) => MONEY.test(column.innerText || '') && !PER_PERIOD.test(column.innerText || ''))
        || rest[rest.length - 1];
      const offer = rest.find((column) => column !== total);

      const aria = checkbox.getAttribute('aria-checked');
      const input = checkbox.matches('input[type="checkbox"]') ? checkbox : checkbox.querySelector('input[type="checkbox"]');
      let checked = null;
      if (aria === 'true' || aria === 'false') {
        checked = aria === 'true';
        if (input && input.checked !== checked) checked = null;
      } else if (input) {
        checked = input.checked === true;
      }

      return [{
        service_column: clean(identity?.innerText || ''),
        offer_column: clean(offer?.innerText || ''),
        total_column: clean(total?.innerText || ''),
        checkbox_checked: checked,
      }];
    });
  })()`), label);
  return parseRenewalCards(cards, label);
}
