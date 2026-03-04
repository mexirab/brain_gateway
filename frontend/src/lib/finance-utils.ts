// Financial Quest Board utility functions

import type { FinanceConfig, LevelInfo, HealthBarStatus } from './finance-types';
import { LEVELS, HEALTH_BAR_THRESHOLDS, xpForLevel } from './finance-constants';

/**
 * Calculate compound interest damage from overspending.
 * Shows what the overspent amount would have been worth at retirement.
 */
export function futureSelfDamage(
  overspend: number,
  config: FinanceConfig
): number {
  const yearsToRetirement = config.retirement_target_age - config.current_age;
  return overspend * Math.pow(1 + config.expected_return, yearsToRetirement);
}

/**
 * Get the level info for a given retirement balance.
 */
export function getLevelForBalance(balance: number): LevelInfo {
  let current = LEVELS[0];
  for (const level of LEVELS) {
    if (balance >= level.retirement_min) {
      current = level;
    } else {
      break;
    }
  }
  return current;
}

/**
 * Get the next level threshold (or null if max level).
 */
export function getNextLevel(currentLevel: number): LevelInfo | null {
  const idx = LEVELS.findIndex((l) => l.level === currentLevel);
  if (idx === -1 || idx >= LEVELS.length - 1) return null;
  return LEVELS[idx + 1];
}

/**
 * Calculate XP progress toward the next level.
 * Returns { current, needed, percent }.
 */
export function xpProgress(totalXP: number, level: number) {
  const currentLevelXP = xpForLevel(level);
  const nextLevelXP = xpForLevel(level + 1);
  const xpInLevel = totalXP - currentLevelXP;
  const xpNeeded = nextLevelXP - currentLevelXP;
  return {
    current: Math.max(0, xpInLevel),
    needed: xpNeeded,
    percent: Math.min(100, Math.max(0, (xpInLevel / xpNeeded) * 100)),
  };
}

/**
 * Determine health bar status based on % spent.
 */
export function getHealthBarStatus(spent: number, budget: number): HealthBarStatus {
  if (budget <= 0) return 'over';
  const ratio = spent / budget;
  if (ratio > 1) return 'over';
  if (ratio > HEALTH_BAR_THRESHOLDS.warning) return 'danger';
  if (ratio > HEALTH_BAR_THRESHOLDS.caution) return 'warning';
  if (ratio > HEALTH_BAR_THRESHOLDS.safe) return 'caution';
  return 'safe';
}

/**
 * Get Tailwind color classes for health bar status.
 */
export function healthBarColor(status: HealthBarStatus): string {
  switch (status) {
    case 'safe': return 'bg-emerald-500';
    case 'caution': return 'bg-yellow-500';
    case 'warning': return 'bg-orange-500';
    case 'danger': return 'bg-red-500';
    case 'over': return 'bg-red-700 animate-pulse';
  }
}

/**
 * Format currency for display.
 */
export function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(amount);
}

/**
 * Format currency with cents.
 */
export function formatCurrencyExact(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

/**
 * Get current year-month string (e.g., "2026-03").
 */
export function currentYearMonth(): string {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  return `${y}-${m}`;
}

/**
 * Calculate projected retirement balance at target age.
 */
export function projectedRetirement(config: FinanceConfig): number {
  const years = config.retirement_target_age - config.current_age;
  const annualContribution = config.savings_rate * config.monthly_discretionary * 12 / config.savings_rate;
  // Simplified: current balance compounded + annual contributions compounded
  const futureBalance = config.retirement_current * Math.pow(1 + config.expected_return, years);
  // Annual contribution of ~$28,685 (from plan)
  const annualSavings = 28_685;
  const futureContributions =
    annualSavings * ((Math.pow(1 + config.expected_return, years) - 1) / config.expected_return);
  return futureBalance + futureContributions;
}

/**
 * Calculate retirement progress as percentage (current / projected).
 */
export function retirementProgress(config: FinanceConfig): number {
  const projected = projectedRetirement(config);
  return Math.min(100, (config.retirement_current / projected) * 100);
}
