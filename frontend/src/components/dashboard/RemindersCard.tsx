'use client';

import { Bell, Check } from 'lucide-react';
import { Card, ErrorState, Skeleton } from '@/components/ui';
import { api } from '@/lib/api';
import { useReminders } from '@/lib/hooks';

export default function RemindersCard() {
  const { data, error, isLoading, mutate } = useReminders();
  const reminders = (data?.reminders ?? []).filter((r) => r.status === 'pending');

  const handleComplete = async (id: string) => {
    // Optimistically drop it, then reconcile with the server.
    const next = data
      ? { ...data, reminders: data.reminders.filter((r) => r.id !== id) }
      : data;
    mutate(next, { revalidate: false });
    try {
      await api.completeReminder(id);
    } finally {
      mutate();
    }
  };

  const formatTime = (iso: string) => {
    return new Date(iso).toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
    });
  };

  return (
    <Card>
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <Bell size={18} className="text-warning" />
        Reminders
        {reminders.length > 0 && (
          <span className="text-xs bg-warning/20 text-warning px-2 py-0.5 rounded-full">
            {reminders.length}
          </span>
        )}
      </h2>

      {isLoading && (
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <Skeleton key={i} className="h-10" />
          ))}
        </div>
      )}

      {!isLoading && error && (
        <ErrorState compact message="Couldn’t load reminders." onRetry={() => mutate()} />
      )}

      {!isLoading && !error && reminders.length === 0 && (
        <p className="text-sm text-content-muted">No pending reminders</p>
      )}

      {!isLoading && !error && reminders.length > 0 && (
        <div className="space-y-2">
          {reminders.map((r) => (
            <div
              key={r.id}
              className="flex items-center gap-3 p-2.5 rounded-lg bg-surface-raised/40 border border-line/30"
            >
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-white truncate">{r.text}</p>
                <p className="text-xs text-content-muted">{formatTime(r.time)}</p>
              </div>
              <button
                onClick={() => handleComplete(r.id)}
                className="p-1.5 rounded-lg hover:bg-success/20 text-content-muted hover:text-success transition-colors shrink-0"
                title="Complete"
              >
                <Check size={16} />
              </button>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
