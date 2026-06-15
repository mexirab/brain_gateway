import Link from 'next/link';
import { Lock } from 'lucide-react';

/** Graceful state shown when a disabled feature's page is hit directly. */
export default function FeatureDisabled({ label }: { label: string }) {
  return (
    <div className="max-w-md mx-auto mt-24 text-center">
      <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-surface-overlay text-zinc-400 mb-4">
        <Lock size={22} />
      </div>
      <h1 className="text-lg font-semibold text-zinc-100 mb-2">
        {label} isn&apos;t enabled
      </h1>
      <p className="text-sm text-zinc-400 mb-6">
        This feature is turned off on this install. An admin can enable it by
        setting the matching flag in <code className="text-zinc-300">.env</code>{' '}
        and restarting the orchestrator.
      </p>
      <Link
        href="/dashboard"
        className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-surface-raised border border-zinc-800 text-zinc-300 hover:text-white hover:bg-surface-overlay transition-colors text-sm"
      >
        Back to Dashboard
      </Link>
    </div>
  );
}
