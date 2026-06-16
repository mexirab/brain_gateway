import { HTMLAttributes } from 'react';

/**
 * Pulsing placeholder for loading states. Replaces the ad-hoc
 * `bg-surface-raised/50 rounded-lg animate-pulse` divs scattered across cards
 * and pages so every skeleton looks the same, reduces layout shift, and
 * respects reduced-motion (the global `globals.css` block neutralizes the
 * pulse). Set size/shape via `className`, e.g. `<Skeleton className="h-10" />`.
 */
export function Skeleton({ className = '', ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden="true"
      className={`animate-pulse rounded-lg bg-surface-raised/50 ${className}`.trim()}
      {...props}
    />
  );
}
