import type {
  HealthResponse,
  FocusState,
  RemindersResponse,
  HAEntitiesResponse,
  CalendarEvent,
  TemperaturesResponse,
  ProgressToday,
  ProgressWeek,
  ProgressStreaks,
  AnnouncementEntry,
  AnnouncementStats,
  AmbientStatus,
  ShoppingItem,
  Task,
  TaskPriority,
  Conversation,
  SavedMessage,
  VaultDocument,
  DocumentCategory,
  WorkoutTodayResponse,
  WorkoutHistorySession,
  GenerateWorkoutResponse,
  ExerciseCatalogEntry,
  WorkoutSet,
  Meal,
  MealsToday,
  MealHistoryResponse,
  MealPhotoEstimate,
  SelfcareTodayResponse,
  ServicesResponse,
} from './types';

const PROXY = '/api/proxy';

async function fetcher<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, init);
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

// Server-side fetcher (uses internal Docker URL)
export async function serverFetch<T>(path: string): Promise<T> {
  const serverBase = process.env.ORCHESTRATOR_URL || 'http://localhost:8888';
  const res = await fetch(`${serverBase}${path}`, { next: { revalidate: 30 } });
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}

export const api = {
  health: () => fetcher<HealthResponse>('/health'),
  services: () => fetcher<ServicesResponse>('/api/services'),
  focus: () => fetcher<FocusState>('/api/focus'),
  startFocus: (task: string, duration: number) =>
    fetcher<{ success: boolean }>('/api/focus/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task, duration }),
    }),
  stopFocus: () =>
    fetcher<{ success: boolean }>('/api/focus/stop', { method: 'POST' }),
  reminders: () => fetcher<RemindersResponse>('/api/reminders'),
  completeReminder: (id: string) =>
    fetcher<{ success: boolean }>(`/api/reminder/complete/${id}`, {
      method: 'POST',
    }),
  entities: () => fetcher<HAEntitiesResponse>('/api/ha/entities'),
  haCommand: (entityId: string, service: string, data?: Record<string, unknown>) =>
    fetcher<{ success: boolean }>('/api/ha/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: entityId, service, data }),
    }),
  calendarToday: () =>
    fetcher<{ events: CalendarEvent[]; source?: string; count?: number }>('/api/calendar/today'),
  temperatures: () => fetcher<TemperaturesResponse>('/api/temperatures'),
  progressToday: () => fetcher<ProgressToday>('/api/progress/today'),
  progressWeek: () => fetcher<ProgressWeek>('/api/progress/week'),
  progressStreaks: () => fetcher<ProgressStreaks>('/api/progress/streaks'),
  announcementHistory: (limit = 20) =>
    fetcher<AnnouncementEntry[]>(`/api/announcements/history?limit=${limit}`),
  announcementStats: () => fetcher<AnnouncementStats>('/api/announcements/stats'),
  clearAnnouncements: () =>
    fetcher<{ ok: boolean; deleted: number }>('/api/announcements/history', {
      method: 'DELETE',
    }),
  ambientStatus: () => fetcher<AmbientStatus>('/api/ambient/status'),
  shoppingList: (listName?: string, includeChecked = false) =>
    fetcher<ShoppingItem[]>(
      `/api/shopping?include_checked=${includeChecked}${listName ? `&list_name=${listName}` : ''}`,
    ),
  addShoppingItem: (item: string, listName = 'grocery') =>
    fetcher<ShoppingItem>('/api/shopping', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item, list_name: listName }),
    }),
  checkShoppingItem: (id: number) =>
    fetcher<{ ok: boolean }>(`/api/shopping/${id}/check`, { method: 'POST' }),
  uncheckShoppingItem: (id: number) =>
    fetcher<{ ok: boolean }>(`/api/shopping/${id}/uncheck`, { method: 'POST' }),
  deleteShoppingItem: (id: number) =>
    fetcher<{ ok: boolean }>(`/api/shopping/${id}`, { method: 'DELETE' }),
  clearCheckedItems: (listName?: string) =>
    fetcher<{ ok: boolean; cleared: number }>(
      `/api/shopping/checked${listName ? `?list_name=${listName}` : ''}`,
      { method: 'DELETE' },
    ),
  // Task backlog
  tasks: (status: 'open' | 'done' | 'dropped' = 'open') =>
    fetcher<Task[]>(`/api/tasks?status=${status}`),
  addTask: (text: string, priority: TaskPriority = 'normal') =>
    fetcher<Task>('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, priority }),
    }),
  completeTask: (id: string) =>
    fetcher<{ ok: boolean }>(`/api/tasks/${id}/complete`, { method: 'POST' }),
  dropTask: (id: string) =>
    fetcher<{ ok: boolean }>(`/api/tasks/${id}/drop`, { method: 'POST' }),
  setTaskPriority: (id: string, priority: TaskPriority) =>
    fetcher<{ ok: boolean }>(`/api/tasks/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ priority }),
    }),
  // Chat conversations
  listConversations: (limit = 50) =>
    fetcher<Conversation[]>(`/api/chat/conversations?limit=${limit}`),
  createConversation: (title: string) =>
    fetcher<Conversation>('/api/chat/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }),
  getConversationMessages: (convId: string) =>
    fetcher<{ conversation: Conversation; messages: SavedMessage[] }>(
      `/api/chat/conversations/${convId}/messages`,
    ),
  saveMessage: (convId: string, role: string, content: string, routing?: unknown, announcementType?: string) =>
    fetcher<SavedMessage>(`/api/chat/conversations/${convId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role, content, routing, announcement_type: announcementType }),
    }),
  updateConversation: (convId: string, title: string) =>
    fetcher<{ ok: boolean }>(`/api/chat/conversations/${convId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }),
  deleteConversation: (convId: string) =>
    fetcher<{ ok: boolean }>(`/api/chat/conversations/${convId}`, { method: 'DELETE' }),
  // Document Vault
  documents: (category?: string, search?: string, limit = 50) =>
    fetcher<VaultDocument[]>(
      `/api/documents?limit=${limit}${category ? `&category=${category}` : ''}${search ? `&search=${encodeURIComponent(search)}` : ''}`,
    ),
  document: (id: string) => fetcher<VaultDocument>(`/api/documents/${id}`),
  uploadDocument: async (file: File, title: string, category: string, tags: string, notes: string) => {
    const form = new FormData();
    form.append('file', file);
    form.append('title', title);
    form.append('category', category);
    form.append('tags', tags);
    form.append('notes', notes);
    const res = await fetch(`${PROXY}/api/documents`, { method: 'POST', body: form });
    if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
    return res.json() as Promise<VaultDocument>;
  },
  updateDocument: (id: string, updates: Partial<VaultDocument>) =>
    fetcher<{ ok: boolean }>(`/api/documents/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    }),
  deleteDocument: (id: string) =>
    fetcher<{ ok: boolean }>(`/api/documents/${id}`, { method: 'DELETE' }),
  documentCategories: () => fetcher<DocumentCategory[]>('/api/documents/categories'),

  // Workouts
  workoutToday: () => fetcher<WorkoutTodayResponse>('/api/workouts/today'),
  workoutHistory: (days = 14) =>
    fetcher<{ days: number; sessions: WorkoutHistorySession[] }>(
      `/api/workouts/history?days=${days}`,
    ),
  generateWorkout: () =>
    fetcher<GenerateWorkoutResponse>('/api/workouts/generate', { method: 'POST' }),
  workoutExercises: () =>
    fetcher<ExerciseCatalogEntry[]>('/api/workouts/exercises'),
  logSet: (body: {
    exercise: string;
    weight_lbs: number;
    reps: number;
    rpe?: number | null;
    set_id?: number;
    workout_id?: number;
  }) =>
    fetcher<{ ok: boolean; workout_id: number; set: WorkoutSet }>(
      '/api/workouts/sets',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
    ),
  modifyWorkout: (
    workoutId: number,
    body: { remove_exercises?: string[]; add_exercises?: string[] },
  ) =>
    fetcher<{ ok: boolean }>(`/api/workouts/${workoutId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  endWorkout: (workoutId: number, notes?: string) =>
    fetcher<{ ok: boolean }>(`/api/workouts/${workoutId}/end`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes }),
    }),
  deleteWorkoutSet: (setId: number) =>
    fetcher<{ ok: boolean }>(`/api/workouts/sets/${setId}`, { method: 'DELETE' }),
  deleteWorkout: (workoutId: number) =>
    fetcher<{ ok: boolean }>(`/api/workouts/${workoutId}`, { method: 'DELETE' }),

  // Meals
  mealsToday: () => fetcher<MealsToday>('/api/meals/today'),
  mealsHistory: (days = 7) =>
    fetcher<MealHistoryResponse>(`/api/meals/history?days=${days}`),
  createMeal: (body: {
    description: string;
    calories?: number | null;
    meal_type?: string;
    photo_path?: string;
    source?: string;
  }) =>
    fetcher<{ ok: boolean; meal: Meal }>('/api/meals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateMeal: (mealId: number, updates: Partial<Meal>) =>
    fetcher<{ ok: boolean; meal: Meal }>(`/api/meals/${mealId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    }),
  deleteMeal: (mealId: number) =>
    fetcher<{ ok: boolean }>(`/api/meals/${mealId}`, { method: 'DELETE' }),
  // Selfcare state
  selfcareToday: () => fetcher<SelfcareTodayResponse>('/api/selfcare/today'),

  uploadMealPhoto: async (file: File, autoLog = false, mealType?: string) => {
    const form = new FormData();
    form.append('file', file);
    form.append('auto_log', autoLog ? 'true' : 'false');
    if (mealType) form.append('meal_type', mealType);
    const res = await fetch(`${PROXY}/api/meals/photo`, {
      method: 'POST',
      body: form,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({} as { error?: string }));
      throw new Error(body.error || `Upload failed: ${res.status}`);
    }
    return res.json() as Promise<MealPhotoEstimate>;
  },
};
