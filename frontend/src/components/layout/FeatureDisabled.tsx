import Link from 'next/link';
import { Lock } from 'lucide-react';

/** Graceful state shown when a disabled feature's page is hit directly. */
export default function FeatureDisabled({ label }: { label: string }) {
  return (
    <div className="max-w-md mx-auto mt-24 text-center">
      <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-surface-overlay text-content-secondary mb-4">
        <Lock size={22} />
      </div>
      <h1 className="text-lg font-semibold text-content-primary mb-2">
        {label} isn&apos;t enabled
      </h1>
      <p className="text-sm text-content-secondary mb-6">
        This feature is turned off on this install. An admin can enable it by
        setting the matching flag in <code className="text-content-primary">.env</code>{' '}
        and restarting the orchestrator.
      </p>
      <Link
        href="/dashboard"
        className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-surface-raised border border-line-subtle text-content-primary hover:text-white hover:bg-surface-overlay transition-colors text-sm"
      >
        Back to Dashboard
      </Link>
    </div>
  );
}
