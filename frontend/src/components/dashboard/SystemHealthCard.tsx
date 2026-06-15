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
      <CheckCircle2 size={14} className="text-success" />
    ) : (
      <XCircle size={14} className="text-danger" />
    );

  return (
    <a href="http://10.0.0.248:3000/d/brain-gateway-overview/brain-gateway-overview" target="_blank" rel="noopener noreferrer" className="block glass p-5 hover:border-brand/40 transition-colors cursor-pointer">
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Activity size={18} className="text-info" />
        System Health
      </h2>

      {loading && <div className="h-24 bg-surface-raised/50 rounded-lg animate-pulse" />}
      {error && <p className="text-sm text-danger/70">Orchestrator offline</p>}

      {!loading && !error && health && (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="text-content-secondary">Orchestrator</span>
            <StatusDot ok={health.ok} />
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-content-secondary">
              {health.architecture === 'unified' ? 'Brain' : 'Helios'}
            </span>
            <StatusDot ok={health.primary_status === 'online'} />
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-content-secondary">
              {health.architecture === 'unified' ? 'Fallback' : 'Nemotron'}
            </span>
            <StatusDot ok={
              health.architecture === 'unified'
                ? health.fallback_status === 'online'
                : health.nemotron_status === 'online'
            } />
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-content-secondary">Scheduler</span>
            <StatusDot ok={health.scheduler.running} />
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-content-secondary">Calendar</span>
            <StatusDot ok={health.calendar.configured} />
          </div>
          <div className="pt-2 border-t border-line-subtle flex items-center justify-between text-xs text-content-muted">
            <span>{health.rag_docs} RAG docs</span>
            <span>{health.ha_entities} HA entities</span>
            <span>v{health.version}</span>
          </div>
        </div>
      )}
    </a>
  );
}
