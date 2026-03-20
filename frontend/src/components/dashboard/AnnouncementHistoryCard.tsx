'use client';

import { useEffect, useState, useCallback } from 'react';
import { Volume2, CheckCircle, XCircle, ArrowRightLeft } from 'lucide-react';
import { api } from '@/lib/api';
import type { AnnouncementEntry, AnnouncementStats } from '@/lib/types';

const TYPE_COLORS: Record<string, string> = {
  calendar: 'text-blue-400',
  briefing: 'text-amber-400',
  reminder: 'text-violet-400',
  focus: 'text-emerald-400',
  routine: 'text-cyan-400',
  progress: 'text-amber-400',
  selfcare: 'text-pink-400',
  ambient: 'text-zinc-400',
  email: 'text-orange-400',
  interrupt: 'text-red-400',
  manual: 'text-zinc-500',
  temperature: 'text-red-400',
  finance: 'text-green-400',
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
  const [history, setHistory] = useState<AnnouncementEntry[]>([]);
  const [stats, setStats] = useState<AnnouncementStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(() => {
    Promise.all([api.announcementHistory(15), api.announcementStats()])
      .then(([h, s]) => {
        setHistory(h);
        setStats(s);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, [fetchData]);

  return (
    <div className="glass p-5">
      <h2 className="text-lg font-semibold text-zinc-300 mb-3 flex items-center gap-2">
        <Volume2 size={18} className="text-blue-400" />
        Announcements
        {stats && (
          <span className="ml-auto text-xs text-zinc-500 font-normal">
            {stats.today_count} today
            {stats.success_rate < 100 && (
              <span className="text-red-400 ml-2">{stats.success_rate}% success</span>
            )}
          </span>
        )}
      </h2>

      {loading && (
        <div className="space-y-2">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-6 bg-zinc-800/50 rounded animate-pulse" />
          ))}
        </div>
      )}
      {error && <p className="text-sm text-red-400/70">{error}</p>}

      {!loading && !error && (
        <div className="space-y-3">
          {/* Stats bar */}
          {stats && stats.total > 0 && (
            <div className="flex gap-4 text-xs text-zinc-500">
              <span className="flex items-center gap-1">
                <CheckCircle size={12} className="text-emerald-400" />
                {stats.successes}
              </span>
              {stats.failures > 0 && (
                <span className="flex items-center gap-1">
                  <XCircle size={12} className="text-red-400" />
                  {stats.failures}
                </span>
              )}
              {stats.fallbacks_used > 0 && (
                <span className="flex items-center gap-1">
                  <ArrowRightLeft size={12} className="text-amber-400" />
                  {stats.fallbacks_used} fallback
                </span>
              )}
              {stats.avg_latency_ms && (
                <span>{(stats.avg_latency_ms / 1000).toFixed(1)}s avg</span>
              )}
            </div>
          )}

          {/* History list */}
          {history.length === 0 && (
            <p className="text-sm text-zinc-600">No announcements yet.</p>
          )}
          <div className="space-y-1.5 max-h-64 overflow-y-auto">
            {history.map((a) => (
              <div
                key={a.id}
                className={`flex items-start gap-2 text-xs ${
                  a.success ? 'text-zinc-400' : 'text-red-400/70'
                }`}
              >
                <span className="text-zinc-600 shrink-0 w-14 text-right">
                  {formatTime(a.timestamp)}
                </span>
                <span
                  className={`shrink-0 w-16 truncate ${
                    TYPE_COLORS[a.announcement_type] || 'text-zinc-500'
                  }`}
                >
                  {a.announcement_type}
                </span>
                <span className="truncate flex-1" title={a.text}>
                  {a.text.length > 80 ? a.text.slice(0, 80) + '...' : a.text}
                </span>
                {a.speaker && (
                  <span className="text-zinc-600 shrink-0 text-[10px]">
                    {speakerShort(a.speaker)}
                  </span>
                )}
                {!a.success && (
                  <XCircle size={12} className="text-red-400 shrink-0" />
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
