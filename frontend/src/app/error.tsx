'use client';

import { useEffect } from 'react';
import { ErrorState } from '@/components/ui';

/** Route-segment error boundary. A render crash in any page now shows this
 *  friendly fallback (with a Reload that re-renders the segment) instead of a
 *  white screen. */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center p-6">
      <ErrorState
        title="This page hit a snag"
        message="Something broke while rendering. Your data is safe — try reloading this section."
        onRetry={reset}
        retryLabel="Reload"
      />
    </div>
  );
}
