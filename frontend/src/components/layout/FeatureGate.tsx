import { getFeatureFlags } from '@/lib/features.server';
import type { FeatureFlags } from '@/lib/features';
import FeatureDisabled from './FeatureDisabled';

/** Server gate: renders children only when the given flag is on, otherwise a
 * graceful "feature not enabled" state. Wraps a route via its layout.tsx so a
 * disabled feature can't be reached by typing the URL directly. */
export default async function FeatureGate({
  flag,
  label,
  children,
}: {
  flag: keyof FeatureFlags;
  label: string;
  children: React.ReactNode;
}) {
  const flags = await getFeatureFlags();
  if (!flags[flag]) return <FeatureDisabled label={label} />;
  return <>{children}</>;
}
