'use client';

import { useEffect } from 'react';
import './globals.css';
import { ErrorState } from '@/components/ui';

/** Last-resort boundary for crashes in the root layout itself. It must render
 *  its own <html>/<body> (it replaces the root layout). Keeps the user on a
 *  branded fallback instead of the browser's raw error page. */
export default function GlobalError({
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
    <html lang="en" className="dark">
      <body className="antialiased min-h-screen">
        <div className="flex min-h-screen items-center justify-center p-6">
          <ErrorState
            title="Something went wrong"
            message="The dashboard ran into an unexpected error. Reloading usually clears it."
            onRetry={reset}
            retryLabel="Reload"
          />
        </div>
      </body>
    </html>
  );
}
