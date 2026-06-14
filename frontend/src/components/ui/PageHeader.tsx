import { ReactNode } from 'react';

interface PageHeaderProps {
  title: string;
  eyebrow?: string;
  description?: string;
  icon?: ReactNode;
  actions?: ReactNode;
}

/** Consistent page title block. Gives every page the same heading rhythm
 *  (eyebrow / title+icon / description) instead of each inventing its own. */
export function PageHeader({ title, eyebrow, description, icon, actions }: PageHeaderProps) {
  return (
    <header className="mb-6 flex items-start justify-between gap-4">
      <div className="min-w-0">
        {eyebrow && <p className="text-eyebrow mb-1">{eyebrow}</p>}
        <div className="flex items-center gap-2">
          {icon && <span className="text-brand">{icon}</span>}
          <h1 className="text-display truncate">{title}</h1>
        </div>
        {description && <p className="text-caption mt-1.5">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}
