'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import {
  Volume2,
  CheckCircle,
  XCircle,
  Trash2,
  Filter,
} from 'lucide-react';
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

const TYPE_BG: Record<string, string> = {
  calendar: 'bg-blue-500/10 border-blue-500/20',
  briefing: 'bg-amber-500/10 border-amber-500/20',
  reminder: 'bg-violet-500/10 border-violet-500/20',
  focus: 'bg-emerald-500/10 border-emerald-500/20',
  routine: 'bg-cyan-500/10 border-cyan-500/20',
  progress: 'bg-amber-500/10 border-amber-500/20',
  selfcare: 'bg-pink-500/10 border-pink-500/20',
  ambient: 'bg-zinc-500/10 border-zinc-500/20',
  email: 'bg-orange-500/10 border-orange-500/20',
  interrupt: 'bg-red-500/10 border-red-500/20',
  manual: 'bg-zinc-500/10 border-zinc-500/20',
  temperature: 'bg-red-500/10 border-red-500/20',
  finance: 'bg-green-500/10 border-green-500/20',
};

function formatTime(ts: string): string {
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString('en', { hour: 'numeric', minute: '2-digit' });
  } catch {
    return ts;
  }
}

function formatDate(ts: string): string {
  try {
    const d = new Date(ts);
    const today = new Date();
    if (d.toDateString() === today.toDateString()) return 'Today';
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
    return d.toLocaleDateString('en', { month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}

function speakerShort(speaker: string | null): string {
  if (!speaker) return '';
  return speaker
    .replace('media_player.', '')
    .replace('snapcast:', '')
    .replace(/_/g, ' ');
}

export default function AnnouncementsPage() {
  const [history, setHistory] = useState<AnnouncementEntry[]>([]);
  const [stats, setStats] = useState<AnnouncementStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const confirmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchData = useCallback(() => {
    Promise.all([api.announcementHistory(200), api.announcementStats()])
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
    const interval = setInterval(fetchData, 15000);
    return () => clearInterval(interval);
  }, [fetchData]);

  useEffect(() => {
    return () => {
      if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
    };
  }, []);

  const handleClear = async () => {
    if (!confirmClear) {
      setConfirmClear(true);
      confirmTimerRef.current = setTimeout(() => setConfirmClear(false), 3000);
      return;
    }
    if (confirmTimerRef.current) clearTimeout(confirmTimerRef.current);
    setClearing(true);
    try {
      await api.clearAnnouncements();
      setHistory([]);
      setStats(null);
      setConfirmClear(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to clear');
    } finally {
      setClearing(false);
    }
  };

  // Get unique types for filter buttons
  const types = Array.from(new Set(history.map((a) => a.announcement_type))).sort();
  const filtered = filter
    ? history.filter((a) => a.announcement_type === filter)
    : history;

  // Group by date
  const grouped: Record<string, AnnouncementEntry[]> = {};
  for (const a of filtered) {
    const key = formatDate(a.timestamp);
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(a);
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-zinc-200 flex items-center gap-3">
          <Volume2 size={24} className="text-blue-400" />
          Announcements
        </h1>
        <button
          onClick={handleClear}
          disabled={clearing || history.length === 0}
          className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm transition-colors ${
            confirmClear
              ? 'bg-red-500/20 text-red-400 border border-red-500/30'
              : 'text-zinc-500 hover:text-red-400 hover:bg-zinc-800/50'
          } disabled:opacity-30 disabled:cursor-not-allowed`}
        >
          <Trash2 size={14} />
          {clearing ? 'Clearing...' : confirmClear ? 'Tap again to confirm' : 'Clear all'}
        </button>
      </div>

      {/* Stats bar */}
      {stats && stats.total > 0 && (
        <div className="glass p-4 flex flex-wrap gap-6 text-sm">
          <div className="flex items-center gap-2">
            <span className="text-zinc-500">Today</span>
            <span className="text-zinc-200 font-medium">{stats.today_count}</span>
          </div>
          <div className="flex items-center gap-2">
            <CheckCircle size={14} className="text-emerald-400" />
            <span className="text-zinc-300">{stats.successes}</span>
          </div>
          {stats.failures > 0 && (
            <div className="flex items-center gap-2">
              <XCircle size={14} className="text-red-400" />
              <span className="text-red-400">{stats.failures}</span>
            </div>
          )}
          {stats.avg_latency_ms && (
            <div className="flex items-center gap-2">
              <span className="text-zinc-500">Avg latency</span>
              <span className="text-zinc-300">
                {(stats.avg_latency_ms / 1000).toFixed(1)}s
              </span>
            </div>
          )}
          <div className="flex items-center gap-2">
            <span className="text-zinc-500">Success</span>
            <span
              className={
                stats.success_rate >= 95 ? 'text-emerald-400' : 'text-amber-400'
              }
            >
              {stats.success_rate}%
            </span>
          </div>
        </div>
      )}

      {/* Type filters */}
      {types.length > 1 && (
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => setFilter(null)}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs transition-colors ${
              filter === null
                ? 'bg-brand-500/20 text-brand-400 border border-brand-500/30'
                : 'text-zinc-500 hover:text-zinc-300 border border-zinc-800'
            }`}
          >
            <Filter size={12} />
            All
          </button>
          {types.map((t) => (
            <button
              key={t}
              onClick={() => setFilter(filter === t ? null : t)}
              className={`px-3 py-1 rounded-full text-xs transition-colors ${
                filter === t
                  ? `${TYPE_BG[t] || 'bg-zinc-500/10 border-zinc-500/20'} ${TYPE_COLORS[t] || 'text-zinc-400'} border`
                  : 'text-zinc-500 hover:text-zinc-300 border border-zinc-800'
              }`}
            >
              {t}
            </button>
          ))}
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div className="space-y-3">
          {[...Array(8)].map((_, i) => (
            <div key={i} className="h-16 bg-zinc-800/30 rounded-lg animate-pulse" />
          ))}
        </div>
      )}
      {error && <p className="text-sm text-red-400/70">{error}</p>}

      {/* Announcement list grouped by date */}
      {!loading && !error && (
        <div className="space-y-6">
          {Object.keys(grouped).length === 0 && (
            <p className="text-sm text-zinc-600 text-center py-12">
              No announcements yet.
            </p>
          )}
          {Object.entries(grouped).map(([date, items]) => (
            <div key={date}>
              <h3 className="text-xs text-zinc-600 font-medium uppercase tracking-wider mb-2">
                {date}
              </h3>
              <div className="space-y-2">
                {items.map((a) => (
                  <div
                    key={a.id}
                    className={`glass p-3 flex items-start gap-3 ${
                      !a.success ? 'border-red-500/20' : ''
                    }`}
                  >
                    {/* Time */}
                    <span className="text-zinc-600 text-xs shrink-0 w-16 pt-0.5">
                      {formatTime(a.timestamp)}
                    </span>

                    {/* Type badge */}
                    <span
                      className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${
                        TYPE_BG[a.announcement_type] ||
                        'bg-zinc-500/10 border-zinc-500/20'
                      } ${TYPE_COLORS[a.announcement_type] || 'text-zinc-400'} border`}
                    >
                      {a.announcement_type}
                    </span>

                    {/* Text */}
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-zinc-300 break-words">{a.text}</p>
                      <div className="flex items-center gap-3 mt-1 text-[10px] text-zinc-600">
                        {a.speaker && <span>{speakerShort(a.speaker)}</span>}
                        {a.latency_ms && (
                          <span>{(a.latency_ms / 1000).toFixed(1)}s</span>
                        )}
                      </div>
                    </div>

                    {/* Status */}
                    {!a.success && (
                      <XCircle size={14} className="text-red-400 shrink-0" />
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
