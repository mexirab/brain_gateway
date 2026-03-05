// API response types matching orchestrator endpoints

export interface HealthResponse {
  ok: boolean;
  version: string;
  architecture: string;
  primary_status: string;
  nemotron_status: string;
  helios_idle: string;
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
