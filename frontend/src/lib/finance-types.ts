// Financial Quest Board types

export interface FinanceConfig {
  monthly_discretionary: number;
  monthly_investing: number;
  monthly_buffer: number;
  retirement_current: number;
  retirement_target_age: number;
  current_age: number;
  savings_rate: number;
  expected_return: number;
}

export interface GameState {
  total_xp: number;
  level: number;
  streak_months: number;
  streak_best: number;
  last_streak_month: string | null;
}

export interface LevelInfo {
  level: number;
  title: string;
  retirement_min: number;
}

export interface XPEvent {
  id: number;
  event_type: XPEventType;
  xp_amount: number;
  description: string;
  created_at: string;
}

export type XPEventType =
  | 'budget_under'
  | 'investment_transfer'
  | 'espp_split'
  | 'bonus_split'
  | 'boss_defeated'
  | 'side_quest_complete'
  | 'quarterly_review'
  | 'streak_milestone'
  | 'perfect_month';

export interface BudgetPeriod {
  year_month: string;
  discretionary_budget: number;
  discretionary_spent: number;
  investing_actual: number;
  boss_battle_active: boolean;
  boss_defeated: boolean;
}

export interface SideQuest {
  id: number;
  name: string;
  description: string | null;
  target_amount: number;
  saved_amount: number;
  monthly_carve: number;
  icon: string;
  status: 'active' | 'completed' | 'abandoned';
  completed_at: string | null;
  created_at: string;
}

export interface Transaction {
  id: number;
  date: string;
  amount: number;
  name: string;
  merchant_name: string | null;
  category: string | null;
  is_discretionary: boolean;
  source: 'ynab' | 'manual';
}

export interface Windfall {
  id: number;
  type: 'bonus' | 'espp';
  amount: number;
  invest_amount: number | null;
  spend_amount: number | null;
  budget_period: string;
  boss_defeated: boolean;
  created_at: string;
}

export type HealthBarStatus = 'safe' | 'caution' | 'warning' | 'danger' | 'over';
