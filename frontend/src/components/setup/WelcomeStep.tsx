'use client';

import { useEffect, useState } from 'react';
import { Loader2, Cpu } from 'lucide-react';
import { setupApi } from '@/lib/setup-api';

type HwState = 'loading' | 'found' | 'absent' | 'error';

export default function WelcomeStep({ onNext }: { onNext: () => void }) {
  const [hw, setHw] = useState<HwState>('loading');

  useEffect(() => {
    let cancelled = false;
    setupApi
      .getHardware()
      .then((r) => {
        if (!cancelled) setHw(r.available ? 'found' : 'absent');
      })
      .catch(() => {
        if (!cancelled) setHw('error');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h2 className="text-xl font-semibold text-white">Welcome to Jess</h2>
        <p className="text-sm text-zinc-400">
          Jess is your personal AI assistant. This quick setup gets the basics
          configured — everything here can be changed later from Settings.
        </p>
      </div>

      <div className="flex items-start gap-3 rounded-lg border border-zinc-800 p-4 text-sm">
        {hw === 'loading' && (
          <>
            <Loader2 size={16} className="mt-0.5 shrink-0 animate-spin text-zinc-500" />
            <span className="text-zinc-500">Checking hardware…</span>
          </>
        )}
        {hw === 'found' && (
          <>
            <Cpu size={16} className="mt-0.5 shrink-0 text-emerald-400" />
            <span className="text-zinc-300">
              Hardware scan detected — model recommendations will be available
              in a later setup step.
            </span>
          </>
        )}
        {hw === 'absent' && (
          <>
            <Cpu size={16} className="mt-0.5 shrink-0 text-amber-400" />
            <span className="text-zinc-400">
              No hardware scan found. You can run{' '}
              <code className="text-zinc-300">
                scripts/detect_hardware.sh --json
              </code>{' '}
              on the host later; the model step will let you configure manually.
            </span>
          </>
        )}
        {hw === 'error' && (
          <>
            <Cpu size={16} className="mt-0.5 shrink-0 text-zinc-500" />
            <span className="text-zinc-500">
              Couldn’t check hardware right now — continuing anyway.
            </span>
          </>
        )}
      </div>

      <div className="flex justify-end">
        <button
          onClick={onNext}
          className="rounded-lg bg-brand-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
        >
          Get started
        </button>
      </div>
    </div>
  );
}
