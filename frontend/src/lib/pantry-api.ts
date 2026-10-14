// Pantry API client — `/api/pantry/*` (F-015). All calls go through
// /api/proxy/[...path], which injects the bearer token.

const PROXY = '/api/proxy';

export type PantryLocation = 'fridge' | 'freezer' | 'pantry';
export type TrackMode = 'count' | 'level' | 'staple';
export type Level = 'full' | 'half' | 'low' | 'out';
export type ItemStatus = 'active' | 'probably_gone' | 'gone';

export const LOCATIONS: PantryLocation[] = ['fridge', 'freezer', 'pantry'];
export const LEVELS: Level[] = ['full', 'half', 'low', 'out'];
export const CATEGORIES = [
  'produce',
  'dairy',
  'protein',
  'grain',
  'legume',
  'canned',
  'condiment',
  'spice',
  'frozen',
  'snack',
  'beverage',
  'bakery',
  'prepared',
  'other',
] as const;
export type Category = (typeof CATEGORIES)[number];

export interface PantryItem {
  id: number;
  name: string;
  category: Category;
  location: PantryLocation;
  track_mode: TrackMode;
  qty: number | null;
  level: Level | null;
  status: ItemStatus;
  shelf_life_days: number | null;
  last_confirmed_at: string;
  barcode: string | null;
  is_out: boolean;
}

export interface ScanLine {
  key: number;
  name: string;
  category: Category;
  location: PantryLocation;
  track_mode: TrackMode;
  qty: number | null;
  level: Level | null;
  confidence: 'high' | 'medium' | 'low';
  action: 'add' | 'update';
  match_id: number | null;
  match_name: string | null;
  include: boolean;
}

export type ScanStatus = 'queued' | 'processing' | 'pending_review' | 'failed' | 'committed' | 'discarded';

export interface PantryScan {
  id: number;
  kind: PantryLocation;
  status: ScanStatus;
  photo_count: number;
  photos_available: boolean;
  proposed: { items: ScanLine[]; warnings: string[] } | null;
  error: string | null;
  created_at: string;
}

export interface BarcodeResult {
  ok: boolean;
  barcode?: string;
  name?: string;
  category?: Category;
  brand?: string | null;
  quantity?: string | null;
  reason?: 'invalid' | 'disabled' | 'miss' | 'error';
  error?: string;
}

export interface NewItem {
  name: string;
  location: PantryLocation;
  category?: Category;
  track_mode?: TrackMode;
  qty?: number | null;
  level?: Level | null;
  barcode?: string | null;
}

/** Error carrying the backend's `{"error": "..."}` message as `detail`, so
 *  `friendlyError(e, fallback, { preferDetail: true })` can show 4xx reasons. */
export class PantryApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`Pantry API ${status}: ${detail || 'request failed'}`);
    this.name = 'PantryApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, init);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}) as { error?: unknown });
    throw new PantryApiError(res.status, typeof body?.error === 'string' ? body.error : '');
  }
  return res.json();
}

function json(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  };
}

export const pantryApi = {
  items: (status: 'active' | 'all' = 'all') =>
    call<{ items: PantryItem[] }>(`/api/pantry/items?status=${status}`).then((r) => r.items),
  add: (item: NewItem) =>
    call<{ ok: boolean; item: PantryItem; merged: boolean }>('/api/pantry/items', json('POST', item)),
  update: (id: number, fields: Partial<Omit<PantryItem, 'id'>>) =>
    call<{ ok: boolean; item: PantryItem }>(`/api/pantry/items/${id}`, json('PATCH', fields)),
  use: (id: number, count?: number) =>
    call<{ ok: boolean; item: PantryItem }>(`/api/pantry/items/${id}/use`, json('POST', { count })),
  out: (id: number) => call<{ ok: boolean; item: PantryItem }>(`/api/pantry/items/${id}/out`, json('POST')),
  remove: (id: number) => call<{ ok: boolean }>(`/api/pantry/items/${id}`, { method: 'DELETE' }),

  scans: (status = '') =>
    call<{ scans: PantryScan[] }>(`/api/pantry/scans${status ? `?status=${status}` : ''}`).then((r) => r.scans),
  scan: (id: number) => call<PantryScan>(`/api/pantry/scan/${id}`),
  createScan: (kind: PantryLocation, files: Blob[]) => {
    const form = new FormData();
    form.append('kind', kind);
    files.forEach((f, i) => form.append('files', f, `photo-${i}.jpg`));
    return call<{ ok: boolean; scan_id: number }>('/api/pantry/scan', { method: 'POST', body: form });
  },
  commitScan: (id: number, items: ScanLine[]) =>
    call<{ ok: boolean; added: number; updated: number; skipped: number }>(
      `/api/pantry/scan/${id}/commit`,
      json('POST', { items }),
    ),
  discardScan: (id: number) => call<{ ok: boolean }>(`/api/pantry/scan/${id}/discard`, json('POST')),
  retryScan: (id: number) => call<{ ok: boolean }>(`/api/pantry/scan/${id}/retry`, json('POST')),
  scanPhotoUrl: (id: number, index: number) => `${PROXY}/api/pantry/scan/${id}/photo/${index}`,

  /** Never throws for a lookup miss — returns `{ok: false, reason}`. */
  barcode: async (code: string): Promise<BarcodeResult> => {
    const res = await fetch(`${PROXY}/api/pantry/barcode/${encodeURIComponent(code)}`);
    return res.json().catch(() => ({ ok: false, reason: 'error' as const }));
  },
};

/** "Eggs ×8", "Feta (half)", "Olive oil (have)". */
export function describeAmount(item: Pick<PantryItem, 'track_mode' | 'qty' | 'level' | 'is_out'>): string {
  if (item.is_out) return 'out';
  if (item.track_mode === 'count') return `×${item.qty ?? 0}`;
  if (item.track_mode === 'staple') return 'have';
  return item.level ?? 'full';
}
