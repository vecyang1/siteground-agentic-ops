const PROVIDER_ID = /^[A-Za-z0-9_-]{8,128}$/;


function number(value) {
  const parsed = Number(String(value ?? '').replaceAll(',', '').trim());
  return Number.isFinite(parsed) ? parsed : null;
}


function percent(used, limit) {
  if (!Number.isFinite(used) || !Number.isFinite(limit) || limit <= 0) return null;
  return Number(((used / limit) * 100).toFixed(3));
}


export function normalizePlanId(value) {
  const normalized = String(value ?? '').trim();
  if (!PROVIDER_ID.test(normalized)) {
    throw new TypeError('An exact non-secret provider plan id is required.');
  }
  return normalized;
}


export function parseStatisticsText(text) {
  const normalized = String(text ?? '').replace(/\s+/g, ' ').trim();
  const web = normalized.match(
    /Web Space\s+([\d,.]+)\s*(GB|MB).*?([\d,.]+)\s*(GB|MB)\s+Used.*?([\d,.]+)\s*(GB|MB)\s+Free/i,
  );
  const inodes = normalized.match(
    /Inodes\s+([\d,]+).*?([\d,]+)\s+Used.*?([\d,]+)\s+Free/i,
  );
  if (!web || !inodes) return [];
  const webLimit = number(web[1]);
  const webUsed = number(web[3]);
  const webFree = number(web[5]);
  const inodeLimit = number(inodes[1]);
  const inodeUsed = number(inodes[2]);
  const inodeFree = number(inodes[3]);
  if ([webLimit, webUsed, webFree, inodeLimit, inodeUsed, inodeFree].some((value) => value === null)) {
    return [];
  }
  return [
    { metric: 'web_space_limit', value: webLimit, unit: web[2].toUpperCase() },
    { metric: 'web_space_used', value: webUsed, unit: web[4].toUpperCase() },
    { metric: 'web_space_free', value: webFree, unit: web[6].toUpperCase() },
    { metric: 'web_space_used_percent', value: percent(webUsed, webLimit), unit: 'percent' },
    { metric: 'inodes_limit', value: inodeLimit, unit: 'count' },
    { metric: 'inodes_used', value: inodeUsed, unit: 'count' },
    { metric: 'inodes_free', value: inodeFree, unit: 'count' },
    { metric: 'inodes_used_percent', value: percent(inodeUsed, inodeLimit), unit: 'percent' },
  ];
}


export function parsePaymentMethodLabels(labels) {
  return labels.flatMap((label) => {
    const match = String(label ?? '').replace(/\s+/g, ' ').trim().match(
      /^Payment method\s+(Primary|Alternative)\s+(card|account)\s+(.+?)\s+ending at(?:\s+\d+)?\s+expires at(?:\s+(\S+))?$/i,
    );
    if (!match) return [];
    return [{
      role: match[1][0].toUpperCase() + match[1].slice(1).toLowerCase(),
      method: match[2].toLowerCase(),
      brand: match[3].trim(),
      expires: match[4] ?? '',
    }];
  });
}
