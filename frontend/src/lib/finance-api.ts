// Finance Quest Board API client
// All calls route through /api/proxy/[...path] for auth

import type {
  FinanceConfig,
  GameState,
  BudgetPeriod,
  SideQuest,
  Transaction,
  XPEvent,
  Windfall,
} from './finance-types';

const PROXY = '/api/proxy';

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${PROXY}${path}`);
  if (!res.ok) throw new Error(`Finance API ${res.status}: ${res.statusText}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`Finance API ${res.status}: ${res.statusText}`);
  return res.json();
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Finance API ${res.status}: ${res.statusText}`);
  return res.json();
}

// ---------- API Client ----------

export const financeApi = {
  // Config
  getConfig: () => get<FinanceConfig>('/api/finance/config'),
  updateConfig: (config: Partial<FinanceConfig>) =>
    put<FinanceConfig>('/api/finance/config', config),

  // Game state
  getGameState: () => get<GameState>('/api/finance/game-state'),
  awardXP: (eventType: string, description?: string) =>
    post<{ ok: boolean; xp_awarded: number; total_xp: number; level: number }>(
      '/api/finance/award-xp',
      { event_type: eventType, description },
    ),

  // Budget
  getCurrentBudget: () => get<BudgetPeriod>('/api/finance/budget/current'),
  addManualEntry: (amount: number, name: string, category?: string) =>
    post<{ ok: boolean; transaction_id: number; new_spent: number }>(
      '/api/finance/budget/manual-entry',
      { amount, name, category },
    ),

  // Transactions
  getTransactions: (month?: string) =>
    get<{ transactions: Transaction[] }>(
      `/api/finance/transactions${month ? `?month=${month}` : ''}`,
    ),
  reclassifyTransaction: (transactionId: number, isDiscretionary: boolean) =>
    post<{ ok: boolean }>('/api/finance/transactions/reclassify', {
      transaction_id: transactionId,
      is_discretionary: isDiscretionary,
    }),

  // Side quests
  getSideQuests: () => get<{ quests: SideQuest[] }>('/api/finance/side-quests'),
  createSideQuest: (quest: {
    name: string;
    target_amount: number;
    monthly_carve: number;
    description?: string;
    icon?: string;
  }) => post<SideQuest>('/api/finance/side-quests', quest),
  contributeSideQuest: (questId: number, amount: number) =>
    post<SideQuest>(`/api/finance/side-quests/${questId}/contribute`, { amount }),
  completeSideQuest: (questId: number) =>
    post<SideQuest>(`/api/finance/side-quests/${questId}/complete`, {}),
  abandonSideQuest: (questId: number) =>
    post<SideQuest>(`/api/finance/side-quests/${questId}/abandon`, {}),

  // Future Self Damage
  getFutureDamage: (amount: number) =>
    get<{ overspend: number; damage: number; years: number }>(
      `/api/finance/future-damage?amount=${amount}`,
    ),

  // Windfalls / Boss Battles
  getWindfalls: () => get<{ windfalls: Windfall[] }>('/api/finance/windfalls'),
  createWindfall: (windfall: {
    type: 'bonus' | 'espp';
    amount: number;
    invest_percent: number;
  }) =>
    post<{
      success: boolean;
      type: string;
      amount: number;
      invest_amount: number;
      spend_amount: number;
      boss_defeated: boolean;
    }>('/api/finance/windfalls', windfall),

  // XP history
  getXPHistory: (limit?: number) =>
    get<{ events: XPEvent[] }>(
      `/api/finance/xp-history${limit ? `?limit=${limit}` : ''}`,
    ),

  // TTS announce
  announce: (text: string) =>
    post<{ success: boolean }>('/api/announce', { text }),
};
