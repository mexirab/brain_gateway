'use client';

import { Volume2, CheckCircle, XCircle } from 'lucide-react';
import { Card, ErrorState, Skeleton } from '@/components/ui';
import { useAnnouncementsCard } from '@/lib/hooks';

const TYPE_COLORS: Record<string, string> = {
  calendar: 'text-info',
  briefing: 'text-warning',
  reminder: 'text-brand',
  focus: 'text-success',
  routine: 'text-info',
  progress: 'text-warning',
  selfcare: 'text-accent-violet',
  ambient: 'text-content-secondary',
  email: 'text-warning',
  interrupt: 'text-danger',
  manual: 'text-content-muted',
  temperature: 'text-danger',
  finance: 'text-success',
};

function formatTime(ts: string): string {
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString('en', { hour: 'numeric', minute: '2-digit' });
  } catch {
    return ts;
  }
}

function speakerShort(speaker: string | null): string {
  if (!speaker) return '';
  return speaker.replace('media_player.', '').replace(/_/g, ' ');
}

export default function AnnouncementHistoryCard() {
  const { data, error, isLoading, mutate } = useAnnouncementsCard();
  const history = data?.history ?? [];
  const stats = data?.stats ?? null;

  return (
    <Card>
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Volume2 size={18} className="text-info" />
        Announcements
        {stats && (
          <span className="ml-auto text-xs text-content-muted font-normal">
            {stats.today_count} today
            {stats.success_rate < 100 && (
              <span className="text-danger ml-2">{stats.success_rate}% success</span>
            )}
          </span>
        )}
      </h2>

      {isLoading && (
        <div className="space-y-2">
          {[...Array(4)].map((_, i) => (
            <Skeleton key={i} className="h-6" />
          ))}
        </div>
      )}
      {!isLoading && error && (
        <ErrorState compact message="Couldn’t load announcements." onRetry={() => mutate()} />
      )}

      {!isLoading && !error && (
        <div className="space-y-3">
          {/* Stats bar */}
          {stats && stats.total > 0 && (
            <div className="flex gap-4 text-xs text-content-muted">
              <span className="flex items-center gap-1">
                <CheckCircle size={12} className="text-success" />
                {stats.successes}
              </span>
              {stats.failures > 0 && (
                <span className="flex items-center gap-1">
                  <XCircle size={12} className="text-danger" />
                  {stats.failures}
                </span>
              )}
              {stats.avg_latency_ms && (
                <span>{(stats.avg_latency_ms / 1000).toFixed(1)}s avg</span>
              )}
            </div>
          )}

          {/* History list */}
          {history.length === 0 && (
            <p className="text-sm text-content-muted">No announcements yet.</p>
          )}
          <div className="space-y-1.5 max-h-64 overflow-y-auto">
            {history.map((a) => (
              <div
                key={a.id}
                className={`flex items-start gap-2 text-xs ${
                  a.success ? 'text-content-secondary' : 'text-danger/70'
                }`}
              >
                <span className="text-content-muted shrink-0 w-14 text-right">
                  {formatTime(a.timestamp)}
                </span>
                <span
                  className={`shrink-0 w-16 truncate ${
                    TYPE_COLORS[a.announcement_type] || 'text-content-muted'
                  }`}
                >
                  {a.announcement_type}
                </span>
                <span className="truncate flex-1" title={a.text}>
                  {a.text.length > 80 ? a.text.slice(0, 80) + '...' : a.text}
                </span>
                {a.speaker && (
                  <span className="text-content-muted shrink-0 text-[10px]">
                    {speakerShort(a.speaker)}
                  </span>
                )}
                {!a.success && (
                  <XCircle size={12} className="text-danger shrink-0" />
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}
