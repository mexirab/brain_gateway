// Settings page API client — `/api/config/*`
// All calls route through /api/proxy/[...path] for bearer auth (proxy injects)

const PROXY = '/api/proxy';

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${PROXY}${path}`);
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`Settings API ${res.status}: ${detail || res.statusText}`);
  }
  return res.json();
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`Settings API ${res.status}: ${detail || res.statusText}`);
  }
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`Settings API ${res.status}: ${detail || res.statusText}`);
  }
  return res.json();
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, { method: 'DELETE' });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`Settings API ${res.status}: ${detail || res.statusText}`);
  }
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

// ----- API -----

export const settingsApi = {
  // Identity
  getIdentity: () => get<Identity>('/api/config/identity'),
  updateIdentity: (updates: IdentityUpdate) =>
    put<Identity>('/api/config/identity', updates),

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
