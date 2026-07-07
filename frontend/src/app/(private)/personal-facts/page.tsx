'use client';

import { useCallback, useEffect, useState } from 'react';
import { Pill, FolderKanban, UserRound } from 'lucide-react';
import { Card, PageHeader, Skeleton, ErrorState, Badge } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import {
  settingsApi,
  type PersonalFacts,
  type Medication,
} from '@/lib/settings-api';

function MedList({ title, meds }: { title: string; meds?: Medication[] }) {
  // Array.isArray guard, not `?? []`: the endpoint returns raw YAML, so a
  // hand-edit could make a bucket a bare string/scalar — `.filter` on that
  // would throw and white-screen the page (parity with the backend renderer).
  const items = (Array.isArray(meds) ? meds : []).filter((m) => m?.name);
  if (items.length === 0) return null;
  return (
    <div>
      <h3 className="text-eyebrow mb-2">{title}</h3>
      <ul className="space-y-2">
        {items.map((m, i) => (
          <li key={`${m.name}-${i}`} className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-medium text-content-primary">{m.name}</span>
            {m.dose && <span className="text-sm text-content-secondary">{m.dose}</span>}
            {m.when && <span className="text-sm text-content-muted">· {m.when}</span>}
            {m.notes && <span className="w-full text-sm text-content-muted">{m.notes}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

const PRIORITY_TONE = {
  high: 'danger',
  medium: 'warning',
  normal: 'warning',
  low: 'neutral',
} as const;

export default function PersonalFactsPage() {
  const [data, setData] = useState<PersonalFacts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await settingsApi.getPersonalFacts());
    } catch (e) {
      setError(friendlyError(e, 'Could not load your personal facts.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const meds = data?.medications ?? {};
  const projects = data?.projects ?? {};
  const profile = data?.profile;
  const activeProjects = (Array.isArray(projects.active) ? projects.active : []).filter((p) => p?.name);
  const reminders = meds.reminders;
  const medBuckets = [meds.daily?.morning, meds.daily?.evening, meds.weekly, meds.as_needed];
  const hasMeds = medBuckets.some((b) => Array.isArray(b) && b.some((m) => m?.name));

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        eyebrow="Source of truth"
        title="Personal Facts"
        icon={<Pill size={22} />}
        description="Exactly what Jess reads about you — straight from the authoritative files, not memory. To change anything, tell Jess or use Settings."
      />

      {loading && (
        <div className="space-y-4">
          <Skeleton className="h-48 w-full rounded-xl" />
          <Skeleton className="h-28 w-full rounded-xl" />
        </div>
      )}

      {!loading && error && <ErrorState message={error} onRetry={load} />}

      {!loading && !error && data && (
        <div className="space-y-4">
          {/* Medications */}
          <Card>
            <div className="mb-4 flex items-center gap-2">
              <Pill size={18} className="text-brand" />
              <h2 className="text-lg font-semibold text-content-primary">Medications</h2>
            </div>
            {hasMeds ? (
              <div className="grid gap-5 sm:grid-cols-2">
                <MedList title="Morning" meds={meds.daily?.morning} />
                <MedList title="Evening" meds={meds.daily?.evening} />
                <MedList title="Weekly" meds={meds.weekly} />
                <MedList title="As needed" meds={meds.as_needed} />
              </div>
            ) : (
              <p className="text-sm text-content-muted">No medications on file.</p>
            )}
            {reminders && (reminders.morning || reminders.evening || reminders.refill) && (
              <p className="text-caption mt-4 border-t border-line-subtle pt-3">
                Reminder times — morning {reminders.morning ?? '—'}, evening {reminders.evening ?? '—'}
                {reminders.refill ? `, refill ${reminders.refill}` : ''}
              </p>
            )}
          </Card>

          {/* Active projects */}
          <Card>
            <div className="mb-4 flex items-center gap-2">
              <FolderKanban size={18} className="text-brand" />
              <h2 className="text-lg font-semibold text-content-primary">Active Projects</h2>
            </div>
            {activeProjects.length === 0 ? (
              <p className="text-sm text-content-muted">No active projects.</p>
            ) : (
              <ul className="space-y-2.5">
                {activeProjects.map((p, i) => (
                  <li key={`${p.name}-${i}`} className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-content-primary">{p.name}</span>
                    {p.priority && (
                      <Badge tone={PRIORITY_TONE[p.priority as keyof typeof PRIORITY_TONE] ?? 'neutral'}>
                        {p.priority}
                      </Badge>
                    )}
                    {p.status && <span className="text-sm text-content-muted">{p.status}</span>}
                    {p.goal && <span className="w-full text-sm text-content-muted">{p.goal}</span>}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {/* Profile */}
          {profile && (
            <Card>
              <div className="mb-4 flex items-center gap-2">
                <UserRound size={18} className="text-brand" />
                <h2 className="text-lg font-semibold text-content-primary">Profile</h2>
              </div>
              <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
                {[
                  ['Name', profile.user_name],
                  ['Assistant', profile.assistant_name],
                  ['Timezone', profile.timezone],
                  ['Tone', profile.tone_preference || 'default'],
                  ['ADHD mode', profile.adhd_mode ? 'on' : 'off'],
                ].map(([label, value]) => (
                  <div key={label} className="flex justify-between gap-3">
                    <dt className="text-content-muted">{label}</dt>
                    <dd className="text-content-secondary">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
