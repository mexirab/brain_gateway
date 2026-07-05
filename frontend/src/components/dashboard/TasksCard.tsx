'use client';

import Link from 'next/link';
import { ListTodo, Check, ArrowRight } from 'lucide-react';
import { Card, ErrorState, Skeleton } from '@/components/ui';
import { api } from '@/lib/api';
import { useTasks } from '@/lib/hooks';

export default function TasksCard() {
  const { data, error, isLoading, mutate } = useTasks('open');
  const tasks = data ?? [];
  // The list arrives pre-ordered (high → normal → low, then oldest first), so
  // the first item is the anti-choice-paralysis "start here" pick.
  const [next, ...rest] = tasks;

  const handleComplete = async (id: string) => {
    const optimistic = tasks.filter((t) => t.id !== id);
    mutate(optimistic, { revalidate: false });
    try {
      await api.completeTask(id);
    } finally {
      mutate();
    }
  };

  return (
    <Card>
      <h2 className="text-lg font-semibold text-content-primary mb-3 flex items-center gap-2">
        <ListTodo size={18} className="text-brand-400" />
        <Link href="/tasks" className="hover:underline">
          Tasks
        </Link>
        {tasks.length > 0 && (
          <span className="text-xs bg-brand-500/20 text-brand-400 px-2 py-0.5 rounded-full">
            {tasks.length}
          </span>
        )}
      </h2>

      {isLoading && (
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <Skeleton key={i} className="h-10" />
          ))}
        </div>
      )}

      {!isLoading && error && (
        <ErrorState compact message="Couldn’t load tasks." onRetry={() => mutate()} />
      )}

      {!isLoading && !error && tasks.length === 0 && (
        <p className="text-sm text-content-muted">Nothing on your list — enjoy the clear deck.</p>
      )}

      {!isLoading && !error && next && (
        <div className="space-y-2">
          <div className="flex items-center gap-3 p-2.5 rounded-lg bg-brand-500/5 border border-brand-500/30">
            <ArrowRight size={16} className="text-brand-400 shrink-0" />
            <div className="flex-1 min-w-0">
              <p className="text-[11px] uppercase tracking-wider text-content-muted">Start here</p>
              <p className="text-sm font-medium text-white truncate">{next.text}</p>
            </div>
            <button
              onClick={() => handleComplete(next.id)}
              className="p-1.5 rounded-lg hover:bg-success/20 text-content-muted hover:text-success transition-colors shrink-0"
              title="Complete"
            >
              <Check size={16} />
            </button>
          </div>

          {rest.slice(0, 3).map((t) => (
            <div
              key={t.id}
              className="flex items-center gap-3 p-2.5 rounded-lg bg-surface-raised/40 border border-line/30"
            >
              <span className="flex-1 min-w-0 text-sm text-content-primary truncate">{t.text}</span>
              <button
                onClick={() => handleComplete(t.id)}
                className="p-1.5 rounded-lg hover:bg-success/20 text-content-muted hover:text-success transition-colors shrink-0"
                title="Complete"
              >
                <Check size={16} />
              </button>
            </div>
          ))}

          {tasks.length > 4 && (
            <Link
              href="/tasks"
              className="block text-xs text-content-muted hover:text-brand-400 transition-colors pt-1"
            >
              +{tasks.length - 4} more →
            </Link>
          )}
        </div>
      )}
    </Card>
  );
}
