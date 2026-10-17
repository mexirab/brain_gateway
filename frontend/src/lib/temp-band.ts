// Matches the orchestrator's profile defaults; used only if the API omits them.
export const DEFAULT_THRESHOLDS = { warning: 80, critical: 85 };

export type Band = 'unknown' | 'ok' | 'warning' | 'critical';

/** Same bands as the closet announcements: warning and critical from the profile. */
export function tempBand(temp: number | null | undefined, warning: number, critical: number): Band {
  if (temp == null) return 'unknown';
  if (temp >= critical) return 'critical';
  if (temp >= warning) return 'warning';
  return 'ok';
}

export interface BarGeometry {
  min: number;
  max: number;
  /** Fill width, 0–100 (0 when there is no reading). */
  progress: number;
  /** Where the warning / critical ticks sit on the bar, 0–100. */
  warningPct: number;
  criticalPct: number;
}

/** The bar spans 20° below warning to 5° above critical, so the bands sit
 *  where the closet actually runs. */
export function barGeometry(temp: number | null | undefined, warning: number, critical: number): BarGeometry {
  const min = warning - 20;
  const max = critical + 5;
  const pct = (t: number) => Math.min(100, Math.max(0, ((t - min) / (max - min)) * 100));
  return {
    min,
    max,
    progress: temp == null ? 0 : pct(temp),
    warningPct: pct(warning),
    criticalPct: pct(critical),
  };
}
