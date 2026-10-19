'use client';

import { useInbox } from '@/lib/hooks';

/** Count of reminders / nudges waiting in the chat tray. Renders nothing at 0. */
export default function ChatNavBadge({ className = '' }: { className?: string }) {
  const { data } = useInbox();
  const n = data?.items.length ?? 0;
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
