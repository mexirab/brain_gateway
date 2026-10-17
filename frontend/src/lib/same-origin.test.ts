import { describe, expect, it } from 'vitest';
import { isCrossSiteRequest } from './same-origin';

const h = (o: Record<string, string>) => new Headers(o);

describe('isCrossSiteRequest', () => {
  it('trusts the browser Sec-Fetch-Site label', () => {
    expect(isCrossSiteRequest(h({ 'sec-fetch-site': 'same-origin' }))).toBe(false);
    expect(isCrossSiteRequest(h({ 'sec-fetch-site': 'none' }))).toBe(false);
    // other ports on the same host (Grafana, SearXNG…) are "same-site": refused
    expect(isCrossSiteRequest(h({ 'sec-fetch-site': 'same-site' }))).toBe(true);
    expect(isCrossSiteRequest(h({ 'sec-fetch-site': 'cross-site' }))).toBe(true);
  });

  it('falls back to Origin vs Host when Sec-Fetch-Site is missing', () => {
    expect(isCrossSiteRequest(h({ origin: 'https://dash.example:3001', host: 'dash.example:3001' }))).toBe(false);
    expect(isCrossSiteRequest(h({ origin: 'https://dash.example:3000', host: 'dash.example:3001' }))).toBe(true);
    expect(
      isCrossSiteRequest(h({ origin: 'https://dash.example', host: '127.0.0.1:3001', 'x-forwarded-host': 'dash.example' })),
    ).toBe(false);
    expect(isCrossSiteRequest(h({ origin: 'null', host: 'dash.example' }))).toBe(true);
    expect(isCrossSiteRequest(h({ origin: 'not a url', host: 'dash.example' }))).toBe(true);
  });

  it('lets non-browser clients (no Sec-Fetch-Site, no Origin) through', () => {
    expect(isCrossSiteRequest(h({ host: 'dash.example' }))).toBe(false);
  });
});
