import { ReactNode } from 'react';
import { AlertTriangle, RotateCw } from 'lucide-react';
import { Button } from './Button';

interface ErrorStateProps {
  /** Short, human heading. Defaults to a friendly generic line. */
  title?: string;
  /** One-line explanation in plain language. NEVER pass a raw `err.message`
   *  / "API 500" string here — that's exactly what this component exists to
   *  hide. Use friendly copy ("Can't reach Jess right now"). */
  message?: string;
  /** When provided, renders a Retry button wired to this handler. */
  onRetry?: () => void;
  retryLabel?: string;
  /** Compact inline variant for inside cards/widgets (single row, no big
   *  icon). Default is the centered full-area variant for pages/boundaries. */
  compact?: boolean;
  icon?: ReactNode;
  className?: string;
}

const DEFAULT_TITLE = 'Something went wrong';
const DEFAULT_MESSAGE = "Can't reach Jess right now. It's not you — give it a moment and try again.";

/** Friendly, on-brand error surface. Replaces raw error strings leaking to the
 *  UI. Use `compact` inside cards; full variant for pages and error boundaries. */
export function ErrorState({
  title = DEFAULT_TITLE,
  message = DEFAULT_MESSAGE,
  onRetry,
  retryLabel = 'Try again',
  compact = false,
  icon,
  className = '',
}: ErrorStateProps) {
  if (compact) {
    return (
      <div className={`flex items-center gap-2 text-sm text-danger/80 ${className}`.trim()} role="alert">
        <AlertTriangle size={14} className="shrink-0" />
        <span className="min-w-0">{message}</span>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="ml-auto shrink-0 text-content-muted underline underline-offset-2 hover:text-content-primary"
          >
            {retryLabel}
          </button>
        )}
      </div>
    );
  }

  return (
    <div
      className={`flex flex-col items-center justify-center gap-3 py-10 text-center ${className}`.trim()}
      role="alert"
    >
      <div className="rounded-full bg-danger/10 p-3 text-danger">
        {icon ?? <AlertTriangle size={24} />}
      </div>
      <div>
        <p className="text-title">{title}</p>
        <p className="text-caption mx-auto mt-1 max-w-xs">{message}</p>
      </div>
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          <RotateCw size={14} />
          {retryLabel}
        </Button>
      )}
    </div>
  );
}
