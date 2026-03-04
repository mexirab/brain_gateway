'use client';

import {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  type ReactNode,
} from 'react';
import { financeApi } from './finance-api';
import type {
  FinanceConfig,
  GameState,
  BudgetPeriod,
  SideQuest,
  Transaction,
  XPEvent,
} from './finance-types';
import { DEFAULT_CONFIG } from './finance-constants';

interface FinanceContextValue {
  // Data
  config: FinanceConfig;
  gameState: GameState;
  budget: BudgetPeriod | null;
  transactions: Transaction[];
  sideQuests: SideQuest[];
  xpHistory: XPEvent[];
  loading: boolean;
  error: string | null;

  // Actions
  refresh: () => Promise<void>;
  addExpense: (amount: number, name: string, category?: string) => Promise<void>;
  awardXP: (eventType: string, description?: string) => Promise<number>;

  // XP toast
  lastXPGain: { amount: number; description: string } | null;
  clearXPGain: () => void;
}

const DEFAULT_GAME_STATE: GameState = {
  total_xp: 0,
  level: 1,
  streak_months: 0,
  streak_best: 0,
  last_streak_month: null,
};

const FinanceContext = createContext<FinanceContextValue | null>(null);

export function FinanceProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<FinanceConfig>({ ...DEFAULT_CONFIG });
  const [gameState, setGameState] = useState<GameState>(DEFAULT_GAME_STATE);
  const [budget, setBudget] = useState<BudgetPeriod | null>(null);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [sideQuests, setSideQuests] = useState<SideQuest[]>([]);
  const [xpHistory, setXPHistory] = useState<XPEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastXPGain, setLastXPGain] = useState<{
    amount: number;
    description: string;
  } | null>(null);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [configRes, gameRes, budgetRes, txRes, questRes, xpRes] =
        await Promise.all([
          financeApi.getConfig(),
          financeApi.getGameState(),
          financeApi.getCurrentBudget(),
          financeApi.getTransactions(),
          financeApi.getSideQuests(),
          financeApi.getXPHistory(20),
        ]);
      setConfig(configRes);
      setGameState(gameRes);
      setBudget(budgetRes);
      setTransactions(txRes.transactions);
      setSideQuests(questRes.quests);
      setXPHistory(xpRes.events);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load finance data';
      setError(msg);
      console.error('[FinanceContext] refresh error:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const addExpense = useCallback(
    async (amount: number, name: string, category?: string) => {
      const result = await financeApi.addManualEntry(amount, name, category);
      // Update budget locally for instant feedback
      if (budget) {
        setBudget({ ...budget, discretionary_spent: result.new_spent });
      }
      // Refresh transactions
      const txRes = await financeApi.getTransactions();
      setTransactions(txRes.transactions);
    },
    [budget],
  );

  const awardXP = useCallback(
    async (eventType: string, description?: string): Promise<number> => {
      const result = await financeApi.awardXP(eventType, description);
      setGameState((prev) => ({
        ...prev,
        total_xp: result.total_xp,
        level: result.level,
      }));
      setLastXPGain({
        amount: result.xp_awarded,
        description: description || eventType,
      });
      return result.xp_awarded;
    },
    [],
  );

  const clearXPGain = useCallback(() => setLastXPGain(null), []);

  return (
    <FinanceContext.Provider
      value={{
        config,
        gameState,
        budget,
        transactions,
        sideQuests,
        xpHistory,
        loading,
        error,
        refresh,
        addExpense,
        awardXP,
        lastXPGain,
        clearXPGain,
      }}
    >
      {children}
    </FinanceContext.Provider>
  );
}

export function useFinance() {
  const ctx = useContext(FinanceContext);
  if (!ctx) throw new Error('useFinance must be used within FinanceProvider');
  return ctx;
}
