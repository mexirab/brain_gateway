'use client';

import { useEffect, useState } from 'react';
import { Bell, Check } from 'lucide-react';
import { api } from '@/lib/api';
import type { Reminder } from '@/lib/types';

export default function RemindersCard() {
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchReminders = () => {
    api
      .reminders()
      .then((data) => setReminders(data.reminders.filter((r) => r.status === 'pending')))
      .catch((e) => setError(e.message))
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
    <div className="glass p-5">
      <h2 className="text-lg font-semibold text-zinc-300 mb-3 flex items-center gap-2">
        <Bell size={18} className="text-amber-400" />
        Reminders
        {reminders.length > 0 && (
          <span className="text-xs bg-amber-500/20 text-amber-400 px-2 py-0.5 rounded-full">
            {reminders.length}
          </span>
        )}
      </h2>

      {loading && (
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <div key={i} className="h-10 bg-zinc-800/50 rounded-lg animate-pulse" />
          ))}
        </div>
      )}

      {error && <p className="text-sm text-red-400/70">{error}</p>}

      {!loading && !error && reminders.length === 0 && (
        <p className="text-sm text-zinc-500">No pending reminders</p>
      )}

      {!loading && !error && reminders.length > 0 && (
        <div className="space-y-2">
          {reminders.map((r) => (
            <div
              key={r.id}
              className="flex items-center gap-3 p-2.5 rounded-lg bg-zinc-800/40 border border-zinc-700/30"
            >
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-white truncate">{r.text}</p>
                <p className="text-xs text-zinc-500">{formatTime(r.time)}</p>
              </div>
              <button
                onClick={() => handleComplete(r.id)}
                className="p-1.5 rounded-lg hover:bg-emerald-500/20 text-zinc-500 hover:text-emerald-400 transition-colors shrink-0"
                title="Complete"
              >
                <Check size={16} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
