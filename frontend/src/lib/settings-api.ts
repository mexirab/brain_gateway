// Settings page API client — `/api/config/*`
// All calls route through /api/proxy/[...path] for bearer auth (proxy injects)

const PROXY = '/api/proxy';

/** Error from a `/api/config/*` call. Carries the HTTP `status` and a cleaned,
 *  user-facing `detail` (the backend's FastAPI `{"detail": "..."}` message,
 *  unwrapped from JSON). `friendlyError(e, fallback, { preferDetail: true })`
 *  surfaces `detail` for 4xx — those are recoverable validation messages
 *  (bad cron, unknown category) the user needs to see — while still hiding
 *  raw 5xx/network internals behind the friendly fallback. */
export class SettingsApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string, raw: string) {
    super(`Settings API ${status}: ${raw || detail || 'request failed'}`);
    this.name = 'SettingsApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function fail(res: Response): Promise<never> {
  const raw = await res.text().catch(() => '');
  let detail = '';
  try {
    const parsed = JSON.parse(raw);
    // FastAPI HTTPException → {"detail": "message"}. Only a string detail is
    // safe to show; 422 returns an array of validation objects — skip those.
    if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
  } catch {
    /* body wasn't JSON */
  }
  throw new SettingsApiError(res.status, detail, raw);
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${PROXY}${path}`);
  if (!res.ok) await fail(res);
  return res.json();
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) await fail(res);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) await fail(res);
  return res.json();
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, { method: 'DELETE' });
  if (!res.ok) await fail(res);
  return res.json();
}

// ----- Types -----

export type ToneOption = '' | 'warm' | 'balanced' | 'direct';

export interface Identity {
  assistant_name: string;
  user_name: string;
  adhd_mode: boolean;
  tone_preference: ToneOption;
  timezone: string;
}

export type IdentityUpdate = Partial<Identity>;

export interface ActiveHours {
  start: string;
  end: string;
}

export interface SelfcareCategory {
  enabled?: boolean;
  interval_minutes?: number;
  interval_hours?: number;
  times?: string[];
  active_hours?: ActiveHours;
  message_template?: string;
}

export interface SelfcareSchedule {
  categories: Record<string, SelfcareCategory>;
}

export type Weekday = 'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat' | 'sun';

export interface QuietHours {
  start: string;
  end: string;
  days: Weekday[];
}

export interface RecurringRule {
  id: string;
  text: string;
  cron_expression: string;
  target: 'tts' | 'push' | 'both';
  enabled: number; // 0|1 from sqlite
  days_of_week: string;
  last_fired_at: string | null;
  next_fire_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RecurringRuleInput {
  text: string;
  cron_expression: string;
  target?: 'tts' | 'push' | 'both';
  days_of_week?: Weekday[];
  enabled?: boolean;
}

export type RecurringRuleUpdate = Partial<RecurringRuleInput>;

export interface RoutineStep {
  id: string;
  label: string;
  est_minutes: number;
  skippable: boolean;
}

export interface RoutineTrigger {
  time: string;
  days: Weekday[];
}

export interface Routine {
  display_name: string;
  trigger: RoutineTrigger;
  speaker: string;
  nudge_delay_minutes: number;
  // Per-routine overrides for the global ROUTINE_NUDGE_MAX (default 3)
  // and ROUTINE_AUTO_SKIP (default false). Effective value is what GET
  // returns — the backend already merges global fallback in.
  nudge_max: number;
  auto_skip: boolean;
  steps: RoutineStep[];
}

export interface RoutinesPayload {
  routines: Record<string, Routine>;
}

export interface RoutinesPutResponse extends RoutinesPayload {
  _reload?: {
    loaded: string[];
    rescheduled: string[];
    removed: string[];
    reload_error?: string;
  };
}

export interface SpeakersPayload {
  /** Raw user-typed values. Empty string = "use legacy fallback". */
  routes: Record<string, string>;
  /** Post-fallback effective values. The panel renders these as
   *  placeholder text when `routes[cat]` is empty so the user can see
   *  what would actually be used. */
  effective: Record<string, string>;
  categories: string[];
}

export interface DiscoveredSpeaker {
  entity_id: string;
  friendly_name: string;
  state: string;
}

// ----- Personal facts (read-only "peek" view) -----

export interface Medication {
  name: string;
  dose?: string;
  purpose?: string;
  notes?: string;
  when?: string;
  max_per_day?: string;
}

export interface Medications {
  daily?: { morning?: Medication[]; evening?: Medication[] };
  weekly?: Medication[];
  as_needed?: Medication[];
  reminders?: { morning?: string; evening?: string; refill?: string };
}

export interface Project {
  name: string;
  status?: string;
  priority?: string;
  goal?: string;
  next_steps?: string[];
}

export interface Projects {
  active?: Project[];
  on_hold?: Project[];
  someday_maybe?: Array<string | Project>;
  completed?: Project[];
  parking_lot?: Array<string | Project>;
}

/** Everything the system authoritatively believes about the user's structured
 *  personal facts — sourced from the YAML the model reads (see get_data). */
export interface PersonalFacts {
  medications: Medications;
  projects: Projects;
  profile: Identity;
}

// ----- API -----

export const settingsApi = {
  // Identity
  getIdentity: () => get<Identity>('/api/config/identity'),
  updateIdentity: (updates: IdentityUpdate) =>
    put<Identity>('/api/config/identity', updates),

  // Personal facts (read-only)
  getPersonalFacts: () => get<PersonalFacts>('/api/config/personal-facts'),

  // Selfcare
  getSelfcare: () => get<SelfcareSchedule>('/api/config/selfcare'),
  updateSelfcare: (schedule: SelfcareSchedule) =>
    put<SelfcareSchedule>('/api/config/selfcare', schedule),

  // Quiet hours
  getQuietHours: () => get<QuietHours>('/api/config/quiet_hours'),
  updateQuietHours: (qh: Partial<QuietHours>) =>
    put<QuietHours>('/api/config/quiet_hours', qh),

  // Routines
  getRoutines: () => get<RoutinesPayload>('/api/config/routines'),
  updateRoutines: (payload: RoutinesPayload) =>
    put<RoutinesPutResponse>('/api/config/routines', payload),

  // Speakers
  getSpeakers: () => get<SpeakersPayload>('/api/config/speakers'),
  updateSpeakers: (routes: Record<string, string>) =>
    put<SpeakersPayload>('/api/config/speakers', { routes }),
  discoverSpeakers: () =>
    get<{ speakers: DiscoveredSpeaker[] }>('/api/config/speakers/discover'),

  // Recurring reminders
  listRecurring: () =>
    get<{ rules: RecurringRule[] }>('/api/config/recurring_reminders'),
  createRecurring: (rule: RecurringRuleInput) =>
    post<RecurringRule>('/api/config/recurring_reminders', rule),
  updateRecurring: (id: string, updates: RecurringRuleUpdate) =>
    put<RecurringRule>(`/api/config/recurring_reminders/${id}`, updates),
  deleteRecurring: (id: string) =>
    del<{ ok: boolean; id: string }>(
      `/api/config/recurring_reminders/${id}`,
    ),
};
