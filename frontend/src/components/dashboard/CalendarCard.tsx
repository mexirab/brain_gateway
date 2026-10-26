'use client';

import { AlertTriangle, Calendar, Clock, ExternalLink } from 'lucide-react';
import { Card, ErrorState, Skeleton } from '@/components/ui';
import { useCalendarToday } from '@/lib/hooks';

export default function CalendarCard() {
  const { data, error, isLoading, mutate } = useCalendarToday();
  const events = data?.events ?? [];
  const unavailable = data?.unavailable ?? [];
  // Only http(s) links are rendered, even though the backend already validates.
  const webUrl = data?.web_url && /^https?:\/\//.test(data.web_url) ? data.web_url : null;
  // source 'none' = no calendar answered: either nothing is connected
  // (unavailable empty) or every connected calendar failed.
  const nothingAnswered = data?.source === 'none';
  const unavailableText =
    unavailable.length > 0
      ? `Couldn’t reach your ${unavailable.join(' or ')} calendar${unavailable.length > 1 ? 's' : ''}`
      : '';

  const formatTime = (iso: string, allDay: boolean) => {
    if (allDay) return 'All day';
    return new Date(iso).toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
    });
  };

  return (
    <Card>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold text-content-primary flex items-center gap-2">
          <Calendar size={18} className="text-brand" />
          Today
        </h2>
        {webUrl && (
          <a
            href={webUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="min-h-[44px] px-2 -mr-2 rounded shrink-0 whitespace-nowrap text-xs text-content-muted hover:text-brand flex items-center gap-1 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
          >
            Open
            <span className="sr-only"> calendar (opens in new tab)</span>
            <ExternalLink size={12} aria-hidden />
          </a>
        )}
      </div>

      {!isLoading && !error && unavailableText && (
        <p role="status" className="text-xs text-warning flex items-center gap-1 mb-2">
          <AlertTriangle size={12} aria-hidden className="shrink-0" />
          {nothingAnswered ? `${unavailableText} — today’s events are unknown` : unavailableText}
        </p>
      )}

      {isLoading && (
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-10" />
          ))}
        </div>
      )}

      {!isLoading && error && (
        <ErrorState compact message="Couldn’t load today’s calendar." onRetry={() => mutate()} />
      )}

      {!isLoading && !error && events.length === 0 && !nothingAnswered && (
        <p className="text-sm text-content-muted">No events today</p>
      )}

      {!isLoading && !error && nothingAnswered && unavailable.length === 0 && (
        <p className="text-sm text-content-muted">No calendar connected</p>
      )}

      {!isLoading && !error && events.length > 0 && (
        <div className="space-y-2">
          {events.map((event) => (
            <div
              key={`${event.id}|${event.start}`}
              className="flex items-center gap-3 p-2.5 rounded-lg bg-surface-raised/40 border border-ink/30"
            >
              <Clock size={14} className="text-content-muted shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-content-primary truncate">
                  {event.title}
                </p>
                <p className="text-xs text-content-muted">
                  {formatTime(event.start, event.all_day)}
                  {event.location && ` · ${event.location}`}
                  {event.calendar && ` · ${event.calendar}`}
                </p>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
