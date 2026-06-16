import { ReactNode } from 'react';

interface EmptyStateProps {
  /** Icon shown above the title (e.g. a lucide <FileText />). */
  icon?: ReactNode;
  title: string;
  /** Optional second line — what to do to fill the empty space. */
  description?: string;
  /** Optional call-to-action (e.g. an "Upload" <Button />). */
  action?: ReactNode;
  className?: string;
}

/** Friendly placeholder for "nothing here yet" areas. The companion to
 *  ErrorState — same centered rhythm, neutral (not alarming) tone. */
export function EmptyState({ icon, title, description, action, className = '' }: EmptyStateProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-3 py-10 text-center ${className}`.trim()}
    >
      {icon && <div className="text-content-muted opacity-40">{icon}</div>}
      <div>
        <p className="text-sm font-medium text-content-secondary">{title}</p>
        {description && <p className="text-caption mx-auto mt-1 max-w-xs">{description}</p>}
      </div>
      {action}
    </div>
  );
}
