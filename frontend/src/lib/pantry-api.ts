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

export type ScanKind = PantryLocation | 'receipt';

export interface ScanLine {
  key: number;
  name: string;
  /** Receipt scans: the text as printed ("KS ORG BBY SPIN"). */
  raw?: string;
  /** Receipt scans: how the line was matched — a learned correction, the inventory, or the model's guess. */
  matched_by?: 'alias' | 'inventory' | 'model';
  /** Receipt scans: false for bags, toiletries, cleaning… (start unticked). */
  food?: boolean;
  /** Receipt scans: unchecked grocery-list entries this line will tick off. */
  ticks?: string[];
  category: Category;
  location: PantryLocation;
  track_mode: TrackMode;
  qty: number | null;
  level: Level | null;
  confidence: 'high' | 'medium' | 'low';
  action: 'add' | 'update' | 'restock' | 'not_seen';
  match_id: number | null;
  match_name: string | null;
  include: boolean;
}

export type ScanStatus = 'queued' | 'processing' | 'pending_review' | 'failed' | 'committed' | 'discarded';

export interface CommitResult {
  ok: boolean;
  added: number;
  updated: number;
  skipped: number;
  /** Receipt commits only. */
  aliases_learned?: number;
  grocery_checked?: number;
  /** Stock-takes: expected items the user confirmed are gone. */
  gone?: number;
}

export interface ReceiptMeta {
  store: string | null;
  date: string | null;
  total: number | null;
  items_sold: number | null;
  duplicate_of: number | null;
}

export interface PantryScan {
  id: number;
  kind: ScanKind;
  status: ScanStatus;
  photo_count: number;
  photos_available: boolean;
  proposed: { items: ScanLine[]; warnings: string[]; receipt?: ReceiptMeta } | null;
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
  /** The parsed error body, for structured fields like `duplicate_of`. */
  body: Record<string, unknown>;
  constructor(status: number, detail: string, body: Record<string, unknown> = {}) {
    super(`Pantry API ${status}: ${detail || 'request failed'}`);
    this.name = 'PantryApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, init);
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    throw new PantryApiError(res.status, typeof body?.error === 'string' ? body.error : '', body ?? {});
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
  createScan: (kind: ScanKind, files: Blob[], force = false) => {
    const form = new FormData();
    form.append('kind', kind);
    if (force) form.append('force', 'true');
    files.forEach((f, i) => form.append('files', f, `photo-${i}.jpg`));
    return call<{ ok: boolean; scan_id: number }>('/api/pantry/scan', { method: 'POST', body: form });
  },
  commitScan: (id: number, items: ScanLine[]) =>
    call<CommitResult>(`/api/pantry/scan/${id}/commit`, json('POST', { items })),
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

// ---------------------------------------------------------------------------
// Diet settings + weekly meal plan (F-015 phase 2)
// ---------------------------------------------------------------------------

export type Weekday = 'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat' | 'sun';

export interface DietSettings {
  profile: string;
  servings: number;
  batch_day: Weekday;
  batch_lunch_days: number;
  fresh_dinners_per_week: number;
  max_dinner_minutes: number;
  avoid: string[];
  extra_rules: string[];
  reminders: { prep: boolean; dinners: boolean; prep_time: string; dinner_time: string };
  fridge_check: { enabled: boolean; day: Weekday; time: string };
}

export interface DietProfileOption {
  key: string;
  label: string;
  description: string;
}

export interface RecipeIngredient {
  item: string;
  amount: string;
  have: boolean;
}

export interface Recipe {
  id: number;
  name: string;
  minutes: number | null;
  servings: number;
  steps: string[];
  ingredients: RecipeIngredient[];
  est_kcal: number | null;
  est_satfat_g: number | null;
  est_fiber_g: number | null;
  rating: number | null;
}

export interface PlanEntry {
  id: number;
  day: string;
  slot: 'prep' | 'lunch' | 'dinner';
  recipe_id: number;
  mode: 'batch' | 'portion' | 'fresh';
  portions: number | null;
  status: 'planned' | 'cooked' | 'eaten' | 'skipped';
  prep_batch_id: number | null;
}

export interface PrepBatch {
  id: number;
  recipe_name: string;
  portions_made: number;
  portions_left: number;
  good_until: string;
  status: 'active' | 'finished' | 'expired' | 'undone';
  expired: boolean;
}

export interface CookResult {
  ok: boolean;
  recipe: string;
  slot: 'prep' | 'dinner';
  deducted: string[];
  running_low: string[];
  not_tracked: string[];
  portions: number | null;
}

export interface OtherCookResult {
  ok: boolean;
  cook_id: number;
  dish: string;
  deducted: string[];
  running_low: string[];
  not_tracked: string[];
}

/** A meal cooked outside the plan (GET /api/pantry/cooked-other). */
export interface OtherCook {
  id: number;
  dish: string;
  source: string;
  cooked_at: string;
  status: 'active' | 'undone';
  items: string[];
  can_undo: boolean;
}

export type PlanStatus = 'generating' | 'draft' | 'active' | 'failed' | 'discarded' | 'replaced';

export interface MealPlan {
  id: number;
  week_start: string;
  status: PlanStatus;
  profile_label: string;
  notes: string | null;
  warnings: string[];
  error: string | null;
  created_at: string;
  entries: PlanEntry[];
  recipes: Record<string, Recipe>;
  batches: Record<string, { portions_made: number; portions_left: number; good_until: string; status: string }>;
  missing: string[];
}

export const planApi = {
  diet: () => call<{ settings: DietSettings; profiles: DietProfileOption[] }>('/api/pantry/diet'),
  saveDiet: (changes: Partial<DietSettings>) =>
    call<{ ok: boolean; settings: DietSettings; rescheduled_plans: number }>('/api/pantry/diet', json('PUT', changes)),
  current: () => call<{ active: MealPlan | null; draft: MealPlan | null }>('/api/pantry/plan'),
  generate: () => call<{ ok: boolean; plan_id: number }>('/api/pantry/plan/generate', json('POST')),
  accept: (id: number, alreadyHave: string[]) =>
    call<{ ok: boolean; added_to_list: number; added_to_pantry: number; reminders: number }>(
      `/api/pantry/plan/${id}/accept`,
      json('POST', { already_have: alreadyHave }),
    ),
  discard: (id: number) => call<{ ok: boolean }>(`/api/pantry/plan/${id}/discard`, json('POST')),
  swap: (entryId: number) =>
    call<{ ok: boolean; plan: MealPlan; added_to_list: number }>(
      `/api/pantry/plan/entry/${entryId}/swap`,
      json('POST'),
    ),
  cooked: (entryId: number) => call<CookResult>(`/api/pantry/plan/entry/${entryId}/cooked`, json('POST')),
  uncook: (entryId: number) =>
    call<{ ok: boolean; restored: number }>(`/api/pantry/plan/entry/${entryId}/uncook`, json('POST')),
  skip: (entryId: number) => call<{ ok: boolean }>(`/api/pantry/plan/entry/${entryId}/skip`, json('POST')),
  prep: () => call<{ batches: PrepBatch[] }>('/api/pantry/prep').then((r) => r.batches),
  eat: (batchId: number) =>
    call<{ ok: boolean; recipe: string; portions_left: number; good_until: string }>(
      `/api/pantry/prep/${batchId}/eat`,
      json('POST'),
    ),
  uneat: (batchId: number) =>
    call<{ ok: boolean; recipe: string; portions_left: number; portion_restored: boolean }>(
      `/api/pantry/prep/${batchId}/uneat`,
      json('POST'),
    ),
  finishBatch: (batchId: number) => call<{ ok: boolean }>(`/api/pantry/prep/${batchId}/finish`, json('POST')),
  cookedOther: (itemIds: number[], dish: string) =>
    call<OtherCookResult>('/api/pantry/cooked-other', json('POST', { item_ids: itemIds, dish })),
  recentOther: () => call<{ cooks: OtherCook[] }>('/api/pantry/cooked-other').then((r) => r.cooks),
  undoOther: (cookId: number) =>
    call<{ ok: boolean; restored: number }>(`/api/pantry/cooked-other/${cookId}/undo`, json('POST')),
  rate: (recipeId: number, rating: 'up' | 'down' | 'clear') =>
    call<{ ok: boolean }>(`/api/pantry/recipe/${recipeId}/rate`, json('POST', { rating })),
};
