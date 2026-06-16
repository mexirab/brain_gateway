'use client';

import { useEffect, useState } from 'react';
import { Bell, Check } from 'lucide-react';
import { Card, ErrorState } from '@/components/ui';
import { api } from '@/lib/api';
import type { Reminder } from '@/lib/types';

export default function RemindersCard() {
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchReminders = () => {
    setError(null);
    setLoading(true);
    api
      .reminders()
      .then((data) => setReminders(data.reminders.filter((r) => r.status === 'pending')))
      .catch(() => setError('Couldn’t load reminders.'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchReminders();
  }, []);

  const handleComplete = async (id: string) => {
    try {
      await api.completeReminder(id);
      setReminders((prev) => prev.filter((r) => r.id !== id));
    } catch {
      // silently fail
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

      {loading && (
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <div key={i} className="h-10 bg-surface-raised/50 rounded-lg animate-pulse" />
          ))}
        </div>
      )}

      {!loading && error && (
        <ErrorState compact message={error} onRetry={fetchReminders} />
      )}

      {!loading && !error && reminders.length === 0 && (
        <p className="text-sm text-content-muted">No pending reminders</p>
      )}

      {!loading && !error && reminders.length > 0 && (
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
