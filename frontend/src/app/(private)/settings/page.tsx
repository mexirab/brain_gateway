'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { User, Heart, Moon, Repeat, Sunrise, Volume2, Dumbbell, AudioLines } from 'lucide-react';
import IdentityPanel from '@/components/settings/IdentityPanel';
import SelfcarePanel from '@/components/settings/SelfcarePanel';
import QuietHoursPanel from '@/components/settings/QuietHoursPanel';
import RecurringRemindersPanel from '@/components/settings/RecurringRemindersPanel';
import RoutinesPanel from '@/components/settings/RoutinesPanel';
import SpeakersPanel from '@/components/settings/SpeakersPanel';
import VoiceStylePanel from '@/components/settings/VoiceStylePanel';
import WorkoutsPanel from '@/components/settings/WorkoutsPanel';
import type { LucideIcon } from 'lucide-react';

// Exported so each panel imports the same union — adding another panel
// is a single-file change here, not a hand-edit across every component.
export type PanelKey =
  | 'identity'
  | 'selfcare'
  | 'quiet'
  | 'routines'
  | 'speakers'
  | 'voice'
  | 'recurring'
  | 'workouts';

interface Tab {
  key: PanelKey;
  label: string;
  icon: LucideIcon;
  description: string;
}

const TABS: Tab[] = [
  { key: 'identity', label: 'Identity & Tone', icon: User, description: 'Names, ADHD mode, tone' },
  { key: 'selfcare', label: 'Selfcare Nudges', icon: Heart, description: 'Meals, water, meds, movement' },
  { key: 'quiet', label: 'Quiet Hours', icon: Moon, description: 'Do-not-disturb window' },
  { key: 'routines', label: 'Routines', icon: Sunrise, description: 'Morning + evening step-by-step flows' },
  { key: 'speakers', label: 'Speakers', icon: Volume2, description: 'Per-category speaker routing' },
  { key: 'voice', label: 'Voice Style', icon: AudioLines, description: 'How Jess sounds in each situation' },
  { key: 'recurring', label: 'Recurring Reminders', icon: Repeat, description: 'Schedules + cron rules' },
  { key: 'workouts', label: 'Workouts', icon: Dumbbell, description: 'Program + gym equipment' },
];

// Each panel calls `registerDirty(panelKey, isDirty)` in an effect so the
// shell can warn before a tab switch destroys their unsaved edits.
export type DirtyRegister = (panel: PanelKey, dirty: boolean) => void;

export default function SettingsPage() {
  const [active, setActive] = useState<PanelKey>('identity');
  // Don't trigger re-renders on dirty changes — only the navigation
  // handler reads this. Using a ref keeps panel rerenders free of
  // shell-state churn.
  const dirtyRef = useRef<Record<PanelKey, boolean>>({
    identity: false,
    selfcare: false,
    quiet: false,
    routines: false,
    speakers: false,
    voice: false,
    recurring: false,
    workouts: false,
  });
  // Hide the Workouts tab when the feature is off. Fail-open (show it) if the
  // flags can't be read — the panel itself handles the disabled 404.
  // null = not known yet: render the tab only once resolved so the rail
  // doesn't shift when it disappears.
  const [workoutsEnabled, setWorkoutsEnabled] = useState<boolean | null>(null);
  useEffect(() => {
    fetch('/api/proxy/api/config/features')
      .then((r) => (r.ok ? r.json() : null))
      .then((f) => setWorkoutsEnabled(!(f && f.workouts_enabled === false)))
      .catch(() => setWorkoutsEnabled(true));
  }, []);
  const tabs = TABS.filter((t) => t.key !== 'workouts' || workoutsEnabled === true);

  const registerDirty = useCallback<DirtyRegister>((panel, dirty) => {
    dirtyRef.current[panel] = dirty;
  }, []);

  const trySwitch = useCallback(
    (target: PanelKey) => {
      if (target === active) return;
      if (dirtyRef.current[active]) {
        const ok = window.confirm(
          'You have unsaved changes in this panel. Discard them and switch?',
        );
        if (!ok) return;
        dirtyRef.current[active] = false;
      }
      setActive(target);
    },
    [active],
  );

  return (
    <div className="max-w-5xl mx-auto">
      <header className="mb-6">
        <h1 className="text-2xl font-bold text-white">Settings</h1>
        <p className="text-sm text-content-secondary mt-1">
          Configure how Jess behaves. Changes apply immediately — no restart.
        </p>
      </header>

      <div className="grid md:grid-cols-[220px_1fr] gap-6">
        {/* Tab rail */}
        <nav aria-label="Settings sections" className="space-y-1">
          {tabs.map(({ key, label, icon: Icon, description }) => {
            const isActive = key === active;
            return (
              <button
                key={key}
                type="button"
                onClick={() => trySwitch(key)}
                aria-current={isActive ? 'page' : undefined}
                className={`w-full text-left flex items-start gap-3 px-3 py-2 rounded-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${
                  isActive
                    ? 'bg-brand-500/10 border border-brand-500/40 text-brand-500'
                    : 'border border-transparent text-content-primary hover:bg-surface-raised/40 hover:text-white'
                }`}
              >
                <Icon size={18} className="mt-0.5 flex-shrink-0" />
                <span className="flex flex-col min-w-0">
                  <span className="text-sm font-medium">{label}</span>
                  <span className="text-xs text-content-muted truncate">{description}</span>
                </span>
              </button>
            );
          })}
        </nav>

        {/* Active panel */}
        <section className="glass p-5">
          {active === 'identity' && <IdentityPanel registerDirty={registerDirty} />}
          {active === 'selfcare' && <SelfcarePanel registerDirty={registerDirty} />}
          {active === 'quiet' && <QuietHoursPanel registerDirty={registerDirty} />}
          {active === 'routines' && <RoutinesPanel registerDirty={registerDirty} />}
          {active === 'speakers' && <SpeakersPanel registerDirty={registerDirty} />}
          {active === 'voice' && <VoiceStylePanel registerDirty={registerDirty} />}
          {active === 'recurring' && <RecurringRemindersPanel registerDirty={registerDirty} />}
          {active === 'workouts' && <WorkoutsPanel registerDirty={registerDirty} />}
        </section>
      </div>
    </div>
  );
}
