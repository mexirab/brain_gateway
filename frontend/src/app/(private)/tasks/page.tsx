'use client';

import { useState, useRef } from 'react';
import { ListTodo, Plus, Check, X, Flag, ArrowRight } from 'lucide-react';
import { api } from '@/lib/api';
import { Card, Button, ErrorState, Skeleton } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import { useTasks } from '@/lib/hooks';
import type { Task, TaskPriority } from '@/lib/types';

const PRIORITY_CYCLE: Record<TaskPriority, TaskPriority> = {
  low: 'normal',
  normal: 'high',
  high: 'low',
};

function priorityStyle(p: TaskPriority): string {
  if (p === 'high') return 'text-danger';
  if (p === 'low') return 'text-content-muted/50';
  return 'text-content-muted';
}

export default function TasksPage() {
  const [newTask, setNewTask] = useState('');
  const [priority, setPriority] = useState<TaskPriority>('normal');
  const [adding, setAdding] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const { data: tasks = [], error: loadError, isLoading, mutate } = useTasks('open');

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = newTask.trim();
    if (!trimmed) return;
    setAdding(true);
    setActionError(null);
    try {
      await api.addTask(trimmed, priority);
      setNewTask('');
      setPriority('normal');
      mutate();
      inputRef.current?.focus();
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t add that task.'));
    } finally {
      setAdding(false);
    }
  };

  const handleComplete = async (id: string) => {
    setActionError(null);
    mutate(tasks.filter((t) => t.id !== id), { revalidate: false });
    try {
      await api.completeTask(id);
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t complete that task.'));
    } finally {
      mutate();
    }
  };

  const handleDrop = async (id: string) => {
    setActionError(null);
    mutate(tasks.filter((t) => t.id !== id), { revalidate: false });
    try {
      await api.dropTask(id);
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t drop that task.'));
    } finally {
      mutate();
    }
  };

  const cyclePriority = async (task: Task) => {
    setActionError(null);
    const next = PRIORITY_CYCLE[task.priority];
    mutate(
      tasks.map((t) => (t.id === task.id ? { ...t, priority: next } : t)),
      { revalidate: false },
    );
    try {
      await api.setTaskPriority(task.id, next);
    } catch (err) {
      setActionError(friendlyError(err, 'Couldn’t change priority.'));
    } finally {
      mutate();
    }
  };

  // Backend already returns high→normal→low, oldest-first. The first item is
  // what `what_now` would surface.
  const next = tasks[0];

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-content-primary flex items-center gap-3">
          <ListTodo size={24} className="text-brand-400" />
          Tasks
        </h1>
        <span className="text-sm text-content-muted">
          {tasks.length} open
        </span>
      </div>

      {/* Add task form */}
      <form onSubmit={handleAdd} className="glass p-4 flex gap-3">
        <input
          ref={inputRef}
          type="text"
          value={newTask}
          onChange={(e) => setNewTask(e.target.value)}
          placeholder="Add a task…"
          className="input flex-1"
          disabled={adding}
        />
        <select
          value={priority}
          onChange={(e) => setPriority(e.target.value as TaskPriority)}
          className="input w-auto shrink-0"
          aria-label="Priority"
        >
          <option value="low">Someday</option>
          <option value="normal">Normal</option>
          <option value="high">High</option>
        </select>
        <Button type="submit" variant="primary" disabled={adding || !newTask.trim()}>
          <Plus size={16} />
          Add
        </Button>
      </form>

      {/* Do-this-next callout — the one-thing-at-a-time nudge */}
      {!isLoading && !loadError && next && (
        <Card className="p-4 flex items-center gap-3 border-brand-500/30 bg-brand-500/5">
          <ArrowRight size={18} className="text-brand-400 shrink-0" />
          <div className="flex-1">
            <p className="text-[11px] uppercase tracking-wider text-content-muted">Start here</p>
            <p className="text-content-primary font-medium">{next.text}</p>
          </div>
          <Button size="sm" variant="primary" onClick={() => handleComplete(next.id)}>
            <Check size={14} />
            Done
          </Button>
        </Card>
      )}

      {/* Loading / Error */}
      {isLoading && (
        <div className="space-y-2">
          {[...Array(4)].map((_, i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </div>
      )}
      {!isLoading && loadError && (
        <ErrorState compact message="Couldn’t load your tasks." onRetry={() => mutate()} />
      )}
      {actionError && <p className="text-sm text-danger/70">{actionError}</p>}

      {/* Task list */}
      {!isLoading && !loadError && (
        <div className="space-y-2">
          {tasks.length === 0 && (
            <p className="text-sm text-content-muted text-center py-12">
              Your list is clear — nothing on the backlog. Add a task above or tell Jess.
            </p>
          )}
          {tasks.map((t) => (
            <Card key={t.id} padding="none" className="p-3 flex items-center gap-3 group">
              <button
                onClick={() => handleComplete(t.id)}
                aria-label={`Complete: ${t.text}`}
                className="w-6 h-6 rounded-md border-2 border-line-strong hover:border-success flex items-center justify-center transition-colors shrink-0"
              />
              <span className="flex-1 text-content-primary">{t.text}</span>
              <button
                onClick={() => cyclePriority(t)}
                aria-label={`Priority: ${t.priority} (click to change)`}
                title={`${t.priority} — click to change`}
                className={`shrink-0 transition-colors hover:text-content-primary ${priorityStyle(t.priority)}`}
              >
                <Flag size={15} fill={t.priority === 'high' ? 'currentColor' : 'none'} />
              </button>
              <button
                onClick={() => handleDrop(t.id)}
                aria-label={`Drop: ${t.text}`}
                title="Drop (no guilt)"
                className="opacity-0 group-hover:opacity-100 text-content-muted hover:text-danger transition-all"
              >
                <X size={14} />
              </button>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
