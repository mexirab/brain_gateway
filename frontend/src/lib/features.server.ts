// Server-only fetch of the runtime feature flags. Talks directly to the
// orchestrator with the API_TOKEN bearer (the same token the /api/proxy route
// injects) rather than going through the proxy, since the proxy depends on the
// browser auth cookie which a server component doesn't carry.

import { ALL_FEATURES_ENABLED, type FeatureFlags } from './features';

export async function getFeatureFlags(): Promise<FeatureFlags> {
  const base = process.env.ORCHESTRATOR_URL || 'http://localhost:8888';
  const token = process.env.API_TOKEN || '';
  try {
    const res = await fetch(`${base}/api/config/features`, {
      headers: { Authorization: `Bearer ${token}` },
      next: { revalidate: 30 },
    });
    if (!res.ok) throw new Error(`features ${res.status}`);
    return (await res.json()) as FeatureFlags;
  } catch (err) {
    // Fail open — see ALL_FEATURES_ENABLED rationale.
    console.warn('[features] failed to read flags, showing all:', err);
    return ALL_FEATURES_ENABLED;
  }
}
