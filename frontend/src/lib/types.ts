// API response types matching orchestrator endpoints

export interface HealthResponse {
  ok: boolean;
  version: string;
  architecture: string;
  primary_status: string;
  // v7 unified: fallback_status; v6 hybrid: nemotron_status
  fallback_status?: string;
  nemotron_status?: string;
  // v7: model_idle; v6: helios_idle
  model_idle?: string;
  helios_idle?: string;
  // v7 unified: list of available tool names
  tools?: string[];
  rag_docs: number;
  ha_entities: number;
  calendar: {
    configured: boolean;
    poll_interval_min: number | null;
    morning_briefing: string | null;
  };
  scheduler: {
    running: boolean;
    scheduled_reminders: number;
    timezone: string;
  };
  focus_timer: FocusState;
}

export interface FocusState {
  active: boolean;
  task: string | null;
  elapsed_minutes: number | null;
  remaining_minutes: number | null;
  duration: number | null;
  break_duration: number | null;
  started: string | null;
}

export interface Reminder {
  id: string;
  text: string;
  time: string;
  status: string;
  scheduled: boolean;
}

export interface RemindersResponse {
  count: number;
  scheduler_jobs: number;
  reminders: Reminder[];
}

export interface HAEntity {
  entity_id: string;
  friendly_name: string;
  state: string;
}

export interface HAEntitiesResponse {
  total: number;
  controllable: Record<string, HAEntity[]>;
}

export interface CalendarEvent {
  id: string;
  title: string;
  start: string;
  end: string;
  location: string | null;
  description: string | null;
  all_day: boolean;
  calendar?: string;
  source?: 'phone' | 'google';
}

export interface TemperatureReading {
  temperature: number | null;
  unit?: string;
  friendly_name?: string;
  error?: string;
}

export interface TemperaturesResponse {
  sensors: Record<string, TemperatureReading>;
  delta: number | null;
  estimated_monthly_cooling_cost: number | null;
  timestamp: string;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export interface RoutingInfo {
  intent_mode: string;
  intent_intensity: string;
  intent_tags: string[];
  mode: string;
}

export interface ChatChunk {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: Array<{
    index: number;
    delta: { content?: string };
    finish_reason: string | null;
  }>;
  _routing?: RoutingInfo;
}

// Progress Tracking (F-005)
export interface ProgressToday {
  date: string;
  tasks_completed: number;
  brain_dumps: number;
  focus_sessions: number;
  focus_minutes: number;
  reminders_done: number;
  routine_steps: number;
}

export interface WeekDay {
  date: string;
  tasks_completed: number;
  focus_sessions: number;
  brain_dumps: number;
  focus_minutes: number;
}

export interface ProgressTotals {
  tasks_completed: number;
  brain_dumps: number;
  focus_sessions: number;
  focus_minutes: number;
  reminders_done: number;
  routine_steps: number;
}

export interface ProgressWeek {
  days: WeekDay[];
  totals: ProgressTotals;
  prior_week_totals: ProgressTotals;
  trend: 'up' | 'down' | 'flat';
  best_day: string | null;
}

export interface Streak {
  category: string;
  current: number;
  longest: number;
  last_active: string;
}

export interface ProgressStreaks {
  streaks: Streak[];
}
