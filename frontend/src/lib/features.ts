// Runtime feature flags exposed by the orchestrator at GET /api/config/features.
// Used to hide nav links for features that are disabled on this install (their
// API routes 404, so the links would be dead). This module is pure/isomorphic
// (no env, no fetch) so it can be imported from both the server layout and the
// client MobileNav. The actual fetch lives in features.server.ts.

export interface FeatureFlags {
  workouts_enabled: boolean;
  meals_enabled: boolean;
  jess_advanced: boolean;
}

// Fail-open default: if the flags can't be read, show everything rather than
// hiding features the user enabled. The orchestrator being unreachable breaks
// the rest of the dashboard anyway, so this only matters on a transient blip.
export const ALL_FEATURES_ENABLED: FeatureFlags = {
  workouts_enabled: true,
  meals_enabled: true,
  jess_advanced: true,
};

// Maps a nav href to the flag that must be true for it to appear. Hrefs not
// listed here are always shown.
const NAV_REQUIRES: Record<string, keyof FeatureFlags> = {
  '/workouts': 'workouts_enabled',
  '/meals': 'meals_enabled',
  '/finance': 'jess_advanced',
};

/** True if a nav item's href should be shown given the current flags. */
export function isNavItemEnabled(href: string, flags: FeatureFlags): boolean {
  const required = NAV_REQUIRES[href];
  return required ? flags[required] : true;
}
