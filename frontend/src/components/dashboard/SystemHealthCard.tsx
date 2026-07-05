'use client';

import { Activity, CheckCircle2, XCircle } from 'lucide-react';
import { Card, ErrorState, Skeleton } from '@/components/ui';
import { useHealth } from '@/lib/hooks';

export default function SystemHealthCard() {
  const { data: health, error, isLoading } = useHealth();

  const StatusDot = ({ ok }: { ok: boolean }) =>
    ok ? (
      <CheckCircle2 size={14} className="text-success" />
    ) : (
      <XCircle size={14} className="text-danger" />
    );

  return (
    <Card
      as="a"
      href="http://jupiter-amds.tail74fc4a.ts.net:3000/d/brain-gateway-overview/brain-gateway-overview"
      target="_blank"
      rel="noopener noreferrer"
      className="block hover:border-brand/40 transition-colors cursor-pointer"
    >
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Activity size={18} className="text-info" />
        System Health
      </h2>

      {isLoading && <Skeleton className="h-24" />}
      {!isLoading && error && <ErrorState compact message="Orchestrator offline" />}

      {!isLoading && !error && health && (
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
    </Card>
  );
}
