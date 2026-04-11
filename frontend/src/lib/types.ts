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

// Announcement History
export interface AnnouncementEntry {
  id: number;
  timestamp: string;
  text: string;
  announcement_type: string;
  speaker: string | null;
  success: number;
  error: string | null;
  latency_ms: number | null;
}

export interface AnnouncementStats {
  total: number;
  successes: number;
  failures: number;
  success_rate: number;
  avg_latency_ms: number | null;
  today_count: number;
  by_type: Record<string, { total: number; success: number }>;
  by_speaker: Record<string, { total: number; success: number }>;
}

// Shopping List
export interface ShoppingItem {
  id: number;
  item: string;
  list_name: string;
  checked: number;
  added_at: string;
  checked_at: string | null;
}

// Ambient Awareness (F-010)
export interface AmbientStatus {
  timestamp: string;
  schedule_density: 'clear' | 'light' | 'busy' | 'unknown';
  events_remaining: number;
  next_event: { title: string; start: string; minutes_away: number } | null;
  focus_active: boolean;
  focus_task?: string;
  routine_active: boolean;
  routine_name?: string;
  pending_reminders: number;
  selfcare_overdue: string[];
  active_task: string | null;
  led_color: string;
}

// Document Vault
export interface VaultDocument {
  id: string;
  title: string;
  category: string;
  tags: string;
  notes: string;
  file_name: string;
  file_path: string;
  file_type: string;
  file_size: number;
  extracted_text?: string | null;
  rag_doc_id: string | null;
  uploaded_at: string;
  updated_at: string;
}

export interface DocumentCategory {
  category: string;
  count: number;
}

// Chat Conversations
export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface SavedMessage {
  id: number;
  conversation_id: string;
  role: 'user' | 'assistant';
  content: string;
  routing: string | null;
  announcement_type: string | null;
  created_at: string;
}
