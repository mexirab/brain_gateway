'use client';

import { Suspense, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Button } from '@/components/ui';
import { safeRedirect } from '@/lib/protected-paths';

const NOT_CONFIGURED =
  'Dashboard password not configured. Set DASHBOARD_TOKEN in .env and recreate the frontend.';

function Form({ configured }: { configured: boolean }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState(configured ? '' : NOT_CONFIGURED);
  const [loading, setLoading] = useState(false);
  const router = useRouter();
  const searchParams = useSearchParams();
  // Untrusted query param: only ever a same-origin protected app path.
  const redirect = safeRedirect(searchParams.get('redirect'));

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });

      if (res.ok) {
        router.push(redirect);
        router.refresh();
      } else if (res.status === 503) {
        const data = await res.json().catch(() => ({}));
        setError(data.error || NOT_CONFIGURED);
      } else if (res.status === 403) {
        setError('The browser blocked this sign-in as cross-site. Open the dashboard from its own address and try again.');
      } else if (res.status === 429) {
        setError('Too many attempts. Wait a few minutes and try again.');
      } else {
        setError('Invalid password');
      }
    } catch {
      setError('Connection error');
    } finally {
      setLoading(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="Password"
        className="input px-4 py-3"
        autoFocus
      />
      {error && (
        <p role="alert" className="text-danger text-sm">
          {error}
        </p>
      )}
      <Button
        type="submit"
        variant="primary"
        disabled={loading || !password || !configured}
        className="w-full py-3"
      >
        {loading ? 'Signing in...' : 'Sign In'}
      </Button>
    </form>
  );
}

export default function LoginForm({ configured }: { configured: boolean }) {
  return (
    <Suspense>
      <Form configured={configured} />
    </Suspense>
  );
}
