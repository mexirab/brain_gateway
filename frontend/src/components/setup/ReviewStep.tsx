'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Loader2, CheckCircle2 } from 'lucide-react';
import { settingsApi, type Identity } from '@/lib/settings-api';
import { setupApi } from '@/lib/setup-api';

interface ReviewStepProps {
  /** Identity captured by the Identity step; the shell passes it through so the
   *  summary needs no extra fetch. Null only if the step was somehow skipped. */
  identity: Identity | null;
  onBack: () => void;
}

export default function ReviewStep({ identity: provided, onBack }: ReviewStepProps) {
  const router = useRouter();
  const [identity, setIdentity] = useState<Identity | null>(provided);
  const [loading, setLoading] = useState(provided === null);
  const [finishing, setFinishing] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (provided !== null) return; // shell already supplied it — no fetch needed
    let cancelled = false;
    settingsApi
      .getIdentity()
      .then((id) => {
        if (!cancelled) setIdentity(id);
      })
      .catch(() => {
        // Non-fatal: the summary is best-effort, setup can still complete.
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [provided]);

  async function handleFinish() {
    setError('');
    setFinishing(true);
    try {
      await setupApi.complete();
      // replace (not push) so the completed wizard isn't in browser history.
      router.replace('/dashboard');
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not complete setup');
      setFinishing(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h2 className="text-xl font-semibold text-white">Review &amp; finish</h2>
        <p className="text-sm text-zinc-400">
          You’re all set. Here’s what Jess will start with — everything is
          editable later in Settings.
        </p>
      </div>

      <div className="space-y-2 rounded-lg border border-zinc-800 p-4 text-sm">
        {loading ? (
          <div className="flex items-center gap-2 text-zinc-500">
            <Loader2 size={16} className="animate-spin" />
            Loading summary…
          </div>
        ) : identity ? (
          <>
            <Row label="Assistant name" value={identity.assistant_name} />
            <Row label="Your name" value={identity.user_name} />
            <Row label="Timezone" value={identity.timezone} />
            <Row label="ADHD mode" value={identity.adhd_mode ? 'On' : 'Off'} />
            <Row label="Tone" value={identity.tone_preference || 'Default'} />
          </>
        ) : (
          <p className="text-zinc-500">
            Summary unavailable — you can still finish setup.
          </p>
        )}
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      <div className="flex justify-between">
        <button
          onClick={onBack}
          disabled={finishing}
          className="rounded-md border border-zinc-700 px-4 py-2 text-sm text-zinc-300 transition-colors hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-40"
        >
          Back
        </button>
        <button
          onClick={handleFinish}
          disabled={finishing}
          className="inline-flex items-center gap-2 rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-50"
        >
          {finishing ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <CheckCircle2 size={16} />
          )}
          {finishing ? 'Finishing…' : 'Finish setup'}
        </button>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-zinc-500">{label}</span>
      <span className="text-zinc-200">{value || '—'}</span>
    </div>
  );
}
