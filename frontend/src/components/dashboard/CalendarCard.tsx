'use client';

import { Calendar, Clock } from 'lucide-react';
import { Card, ErrorState } from '@/components/ui';
import { useCalendarToday } from '@/lib/hooks';

export default function CalendarCard() {
  const { data, error, isLoading, mutate } = useCalendarToday();
  const events = data?.events ?? [];
  const source = data?.source ?? '';

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
        {source && (
          <span className="text-[10px] text-content-muted uppercase tracking-wider">
            {source === 'phone' ? 'All calendars' : 'Google'}
          </span>
        )}
      </div>

      {isLoading && (
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-10 bg-surface-raised/50 rounded-lg animate-pulse" />
          ))}
        </div>
      )}

      {!isLoading && error && (
        <ErrorState compact message="Couldn’t load today’s calendar." onRetry={() => mutate()} />
      )}

      {!isLoading && !error && events.length === 0 && (
        <p className="text-sm text-content-muted">No events today</p>
      )}

      {!isLoading && !error && events.length > 0 && (
        <div className="space-y-2">
          {events.map((event) => (
            <div
              key={event.id}
              className="flex items-center gap-3 p-2.5 rounded-lg bg-surface-raised/40 border border-line/30"
            >
              <Clock size={14} className="text-content-muted shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-white truncate">
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
