// Financial Quest Board constants

import type { LevelInfo } from './finance-types';

// ---------- XP Awards ----------
export const XP_AWARDS = {
  budget_under: 100,
  investment_transfer: 50,
  espp_split: 200,
  bonus_split: 200,
  boss_defeated: 200,
  side_quest_complete: 150,
  quarterly_review: 75,
  streak_milestone: 50,
  perfect_month: 50,
} as const;

// ---------- Level Thresholds ----------
// Based on retirement savings milestones
export const LEVELS: LevelInfo[] = [
  { level: 1, title: 'Copper Adventurer', retirement_min: 0 },
  { level: 2, title: 'Bronze Scout', retirement_min: 525_000 },
  { level: 3, title: 'Silver Ranger', retirement_min: 550_000 },
  { level: 4, title: 'Gold Knight', retirement_min: 575_000 },
  { level: 5, title: 'Platinum Warden', retirement_min: 600_000 },
  { level: 6, title: 'Diamond Guardian', retirement_min: 650_000 },
  { level: 7, title: 'Emerald Champion', retirement_min: 700_000 },
  { level: 8, title: 'Sapphire Sovereign', retirement_min: 750_000 },
  { level: 9, title: 'Ruby Archmage', retirement_min: 800_000 },
  { level: 10, title: 'Obsidian Legend', retirement_min: 900_000 },
  { level: 11, title: 'Millionaire Ascendant', retirement_min: 1_000_000 },
];

// ---------- Health Bar Thresholds (% of budget spent) ----------
export const HEALTH_BAR_THRESHOLDS = {
  safe: 0.6,      // 0-60% green
  caution: 0.8,   // 60-80% yellow
  warning: 0.95,  // 80-95% orange
  danger: 1.0,    // 95-100% red
  // over 100% = pulsing dark red
} as const;

// ---------- Default Financial Config ----------
export const DEFAULT_CONFIG = {
  monthly_discretionary: 1000,
  monthly_investing: 400,
  monthly_buffer: 68.75,
  retirement_current: 518_500,
  retirement_target_age: 62,
  current_age: 48,
  savings_rate: 0.20,
  expected_return: 0.07,
} as const;

// ---------- Windfall Months ----------
export const WINDFALL_MONTHS: Record<string, 'bonus' | 'espp'> = {
  '03': 'bonus',  // March
  '06': 'espp',   // June
  '10': 'bonus',  // October
  '12': 'espp',   // December
};

// ---------- XP needed per level (cumulative) ----------
// Simple formula: level N requires N * 200 total XP
export function xpForLevel(level: number): number {
  return level * 200;
}
