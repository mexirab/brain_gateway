'use client';

import { useEffect, useState } from 'react';
import { Check, Monitor, Moon, Sun } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { parseThemePref, setThemePref, type ThemePref } from '@/lib/theme';

const OPTIONS: { value: ThemePref; label: string; hint: string; icon: LucideIcon }[] = [
  { value: 'dark', label: 'Dark', hint: 'Deep ink with violet accents', icon: Moon },
  { value: 'light', label: 'Light', hint: 'Bright, for daytime', icon: Sun },
  { value: 'system', label: 'System', hint: 'Follow this device', icon: Monitor },
];

/** Per-device theme. Applies instantly; nothing is sent to the server. */
export default function AppearancePanel() {
  const [pref, setPref] = useState<ThemePref | null>(null);

  useEffect(() => {
    setPref(parseThemePref(document.documentElement.dataset.theme));
  }, []);

  const choose = (value: ThemePref) => {
    setThemePref(value);
    setPref(value);
  };

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-content-primary">Appearance</h2>
        <p className="text-sm text-content-secondary mt-1">
          Saved on this device only.
        </p>
      </div>

      <div role="radiogroup" aria-label="Theme" className="grid gap-3 sm:grid-cols-3">
        {OPTIONS.map(({ value, label, hint, icon: Icon }) => {
          const selected = pref === value;
          return (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => choose(value)}
              className={`relative flex flex-col items-start gap-2 rounded-card border p-4 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50 ${
                selected
                  ? 'border-brand/60 bg-brand/10'
                  : 'border-line bg-surface-inset/40 hover:border-line-strong'
              }`}
            >
              <span
                aria-hidden
                className="flex h-16 w-full overflow-hidden rounded-lg border border-line"
              >
                {value === 'system' ? (
                  <>
                    <Swatch theme="light" />
                    <Swatch theme="dark" />
                  </>
                ) : (
                  <Swatch theme={value} />
                )}
              </span>
              <span className="flex items-center gap-2 text-sm font-medium text-content-primary">
                <Icon size={16} aria-hidden />
                {label}
              </span>
              <span className="text-xs text-content-muted">{hint}</span>
              {selected && (
                <Check size={16} aria-hidden className="absolute right-3 top-3 text-brand" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** A mini page in the given palette: its own data-theme scopes the tokens. */
function Swatch({ theme }: { theme: 'dark' | 'light' }) {
  return (
    <span data-theme={theme} className="flex flex-1 flex-col gap-1.5 bg-surface-base p-2">
      <span className="h-2 w-1/2 rounded-full bg-content-primary/80" />
      <span className="flex-1 rounded-md border border-line bg-surface-raised" />
      <span className="h-2 w-1/3 rounded-full bg-brand" />
    </span>
  );
}
