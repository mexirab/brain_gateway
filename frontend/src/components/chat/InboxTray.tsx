'use client';

import { useEffect, useState } from 'react';
import { useSWRConfig } from 'swr';
import {
  Activity,
  AlarmClock,
  AlertTriangle,
  Check,
  Clock,
  Footprints,
  GlassWater,
  Moon,
  Pill,
  Sun,
  UtensilsCrossed,
  X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { api } from '@/lib/api';
import { INBOX_KEY, useInbox } from '@/lib/hooks';
import type { InboxItem, InboxResponse } from '@/lib/types';

const SNOOZE_MINUTES = 15;

const SELFCARE_ICONS: Record<string, LucideIcon> = {
  medication: Pill,
  meal: UtensilsCrossed,
  water: GlassWater,
  movement: Footprints,
};

const MESSAGE_ICONS: Record<string, LucideIcon> = {
  morning_briefing: Sun,
  evening_briefing: Moon,
  missed_recap: AlertTriangle,
  self_audit: Activity,
};

function iconFor(item: InboxItem): LucideIcon {
  if (item.kind === 'reminder') return AlarmClock;
  if (item.kind === 'message') return MESSAGE_ICONS[item.ref] ?? AlarmClock;
  return SELFCARE_ICONS[item.ref] ?? AlarmClock;
}

function timeLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  // Cards live up to 24 h — "9:00 PM" from last night must not read as tonight.
  return d.toDateString() === new Date().toDateString() ? time : `Yesterday ${time}`;
}

/** Actionable cards (Done/Snooze) first, then informational ones; each group
 *  keeps the server's newest-first order. A long briefing must not push the
 *  07:00 med reminder below the fold. */
function actionableFirst(items: InboxItem[]): InboxItem[] {
  return [...items.filter((i) => i.kind !== 'message'), ...items.filter((i) => i.kind === 'message')];
}

const ERROR_TEXT: Record<string, string> = {
  max_snoozes_reached: 'Snooze limit reached for this one.',
  reminder_gone: 'That reminder no longer exists.',
};

/**
 * Reminders and self-care nudges waiting for a tap — the dashboard's
 * replacement for the Telegram Done / Snooze buttons. Briefing digests and
 * the missed-reminder recap land here too (kind `message`): read, then
 * "Got it" (dismiss) — there is nothing to log. Shares the `inbox`
 * SWR key with the nav badge, so a tap here clears the badge too.
 */
export default function InboxTray() {
  const { data, error } = useInbox();
  const { mutate } = useSWRConfig();
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  // A notification tap (service worker) or a push while the app is open
  // means there is something new — refetch instead of waiting for the poll.
  useEffect(() => {
    if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
    const onMessage = (e: MessageEvent) => {
      if (e.data?.type === 'inbox-changed') void mutate(INBOX_KEY);
    };
    navigator.serviceWorker.addEventListener('message', onMessage);
    return () => navigator.serviceWorker.removeEventListener('message', onMessage);
  }, [mutate]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(t);
  }, [toast]);

  const items = actionableFirst(data?.items ?? []);
  if (items.length === 0 && !toast && !error) return null;

  async function act(item: InboxItem, action: 'done' | 'snooze' | 'dismiss') {
    setBusy(item.id);
    // Optimistic: the card disappears immediately; a failure puts it back
    // (restored explicitly — a revalidate can't when Jess is unreachable).
    const prev = data;
    const restore = () => mutate(INBOX_KEY, prev, { revalidate: false });
    const without = (cur?: InboxResponse) => ({ items: (cur?.items ?? []).filter((i) => i.id !== item.id) });
    await mutate(INBOX_KEY, without, { revalidate: false });
    try {
      const res = await api.inboxAction(item.id, action, action === 'snooze' ? SNOOZE_MINUTES : undefined);
      if (res.ok) {
        if (action === 'done') setToast('✓ Logged');
        else if (action === 'snooze') setToast(`💤 Snoozed ${SNOOZE_MINUTES} min`);
      } else if (res.error === 'not_found') {
        // Pruned/gone server-side: the card is stale, nothing to retry.
      } else {
        if (res.error !== 'reminder_gone') await restore();
        setToast(ERROR_TEXT[res.error ?? ''] ?? 'Couldn’t save that — try again.');
      }
    } catch {
      await restore();
      setToast('Couldn’t reach Jess — try again.');
    } finally {
      setBusy(null);
      void mutate(INBOX_KEY);
    }
  }

  const btn =
    'inline-flex items-center justify-center gap-1 min-h-11 px-3 rounded-lg text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50 disabled:opacity-50';

  return (
    <section
      aria-label="Waiting for you"
      className="px-4 pb-2 space-y-2 max-h-[30dvh] overflow-y-auto shrink-0 [@media(max-height:500px)]:max-h-[45dvh]"
    >
      {error && items.length === 0 && (
        <p className="text-xs text-content-muted px-1">Can’t load reminders right now.</p>
      )}
      {items.map((item) => {
        const Icon = iconFor(item);
        const disabled = busy === item.id;
        if (item.kind === 'message') {
          // Informational: read, then "Got it". Full-width text with the
          // button below it (where reading ends); long digests clamp.
          const warn = item.ref === 'missed_recap';
          const open = expanded.has(item.id);
          const long = item.text.length > 220 || item.text.split('\n').length > 4;
          return (
            <div
              key={item.id}
              className={`rounded-xl border px-3 py-2 ${warn ? 'border-warning/30 bg-warning/10' : 'border-brand/30 bg-brand/10'}`}
            >
              <div className="flex items-start gap-3">
                <Icon size={18} className={`${warn ? 'text-warning' : 'text-brand'} shrink-0 mt-0.5`} aria-hidden />
                <div className="flex-1 min-w-0">
                  <p
                    className={`text-sm text-content-primary break-words whitespace-pre-line ${long && !open ? 'line-clamp-4' : ''}`}
                  >
                    {item.text}
                  </p>
                  <span className="block mt-1 text-xs text-content-muted">{timeLabel(item.created_at)}</span>
                </div>
              </div>
              <div className="flex items-center justify-end gap-1.5 mt-1">
                {long && (
                  <button
                    type="button"
                    onClick={() =>
                      setExpanded((cur) => {
                        const next = new Set(cur);
                        if (open) next.delete(item.id);
                        else next.add(item.id);
                        return next;
                      })
                    }
                    aria-expanded={open}
                    className={`${btn} text-content-secondary hover:text-content-primary`}
                  >
                    {open ? 'Show less' : 'Show more'}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => act(item, 'dismiss')}
                  disabled={disabled}
                  className={`${btn} bg-surface-overlay/40 text-content-secondary hover:text-content-primary`}
                >
                  <Check size={16} aria-hidden />
                  Got it
                </button>
              </div>
            </div>
          );
        }
        return (
          <div
            key={item.id}
            className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-warning/30 bg-warning/10 px-3 py-2"
          >
            <Icon size={18} className="text-warning shrink-0" aria-hidden />
            <p className="flex-1 min-w-[10rem] text-sm text-content-primary break-words">
              {item.text}
              <span className="ml-2 text-xs text-content-muted">{timeLabel(item.created_at)}</span>
            </p>
            <div className="flex items-center gap-1.5 shrink-0">
              <button
                type="button"
                onClick={() => act(item, 'done')}
                disabled={disabled}
                className={`${btn} bg-success/20 text-success hover:bg-success/30`}
              >
                <Check size={16} aria-hidden />
                Done
              </button>
              {item.kind === 'reminder' && (
                <button
                  type="button"
                  onClick={() => act(item, 'snooze')}
                  disabled={disabled}
                  aria-label={`${SNOOZE_MINUTES}m — snooze ${SNOOZE_MINUTES} minutes`}
                  className={`${btn} bg-surface-overlay/40 text-content-secondary hover:text-content-primary`}
                >
                  <Clock size={16} aria-hidden />
                  {SNOOZE_MINUTES}m
                </button>
              )}
              <button
                type="button"
                onClick={() => act(item, 'dismiss')}
                disabled={disabled}
                aria-label="Dismiss without logging"
                title="Dismiss"
                className={`${btn} min-w-11 px-0 text-content-muted hover:text-content-primary`}
              >
                <X size={16} aria-hidden />
              </button>
            </div>
          </div>
        );
      })}
      <span className="sr-only" role="status" aria-live="polite">
        {toast ?? ''}
      </span>
      {toast && <p className="text-xs text-content-secondary px-1" aria-hidden>{toast}</p>}
    </section>
  );
}
