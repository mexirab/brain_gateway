'use client';

import { useEffect, useState } from 'react';
import { Activity, CheckCircle2, XCircle } from 'lucide-react';
import { api } from '@/lib/api';
import type { HealthResponse } from '@/lib/types';

export default function SystemHealthCard() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const StatusDot = ({ ok }: { ok: boolean }) =>
    ok ? (
      <CheckCircle2 size={14} className="text-emerald-400" />
    ) : (
      <XCircle size={14} className="text-red-400" />
    );

  return (
    <a href="http://10.0.0.248:3000" target="_blank" rel="noopener noreferrer" className="block glass p-5 hover:border-indigo-500/40 transition-colors cursor-pointer">
      <h2 className="text-lg font-semibold text-zinc-300 mb-3 flex items-center gap-2">
        <Activity size={18} className="text-cyan-400" />
        System Health
      </h2>

      {loading && <div className="h-24 bg-zinc-800/50 rounded-lg animate-pulse" />}
      {error && <p className="text-sm text-red-400/70">Orchestrator offline</p>}

      {!loading && !error && health && (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="text-zinc-400">Orchestrator</span>
            <StatusDot ok={health.ok} />
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-zinc-400">Nemotron</span>
            <StatusDot ok={health.primary_status === 'ok'} />
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-zinc-400">Helios</span>
            <span className="text-xs text-zinc-500">{health.helios_idle}</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-zinc-400">Scheduler</span>
            <StatusDot ok={health.scheduler.running} />
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-zinc-400">Calendar</span>
            <StatusDot ok={health.calendar.configured} />
          </div>
          <div className="pt-2 border-t border-zinc-800 flex items-center justify-between text-xs text-zinc-500">
            <span>{health.rag_docs} RAG docs</span>
            <span>{health.ha_entities} HA entities</span>
            <span>v{health.version}</span>
          </div>
        </div>
      )}
    </a>
  );
}
