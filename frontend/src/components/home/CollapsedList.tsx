'use client';

import { useState, type ReactNode } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { Card } from '@/components/ui';

/** A closed-by-default card for the noisy tail (device settings, unavailable). */
export default function CollapsedList({
  title,
  hint,
  count,
  children,
}: {
  title: string;
  hint?: string;
  count: number;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  if (count === 0) return null;
  return (
    <Card padding="none" className="overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex items-center gap-2 w-full min-h-[48px] px-4 py-3 text-left"
      >
        {open ? <ChevronDown size={16} className="text-content-muted" /> : <ChevronRight size={16} className="text-content-muted" />}
        <span className="text-sm font-semibold text-content-secondary flex-1">{title}</span>
        <span className="text-xs text-content-muted">{count}</span>
      </button>
      {open && (
        <div className="px-4 pb-4 space-y-2">
          {hint && <p className="text-xs text-content-muted">{hint}</p>}
          {children}
        </div>
      )}
    </Card>
  );
}
