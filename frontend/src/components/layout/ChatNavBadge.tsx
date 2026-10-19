'use client';

import { useInbox } from '@/lib/hooks';

/** Count of reminders / nudges waiting in the chat tray. Renders nothing at 0.
 *  Briefing digests (kind `message`) don't count — one sits there most of the
 *  day and an always-on badge gets ignored. The missed-reminder recap does. */
export default function ChatNavBadge({ className = '' }: { className?: string }) {
  const { data } = useInbox();
  const n = (data?.items ?? []).filter((i) => i.kind !== 'message' || i.ref === 'missed_recap').length;
  if (n === 0) return null;
  return (
    <span
      className={`inline-flex items-center justify-center min-w-[1.125rem] h-[1.125rem] px-1 rounded-full bg-warning text-[10px] font-bold text-surface-base leading-none ${className}`}
    >
      {n > 9 ? '9+' : n}
      <span className="sr-only"> waiting</span>
    </span>
  );
}
