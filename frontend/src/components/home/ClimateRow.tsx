'use client';

import { Minus, Plus, Thermometer } from 'lucide-react';
import type { HAEntity } from '@/lib/types';
import { Button } from '@/components/ui';
import { useHomeControls } from './HomeContext';

/** Thermostat: current reading + target with −/+ (one degree per tap). */
export default function ClimateRow({ entity }: { entity: HAEntity }) {
  const { run, errors, pending } = useHomeControls();
  const target = entity.temperature;
  const error = errors[entity.entity_id];
  const busy = pending[entity.entity_id];

  const nudge = (delta: number) => {
    if (typeof target !== 'number') return;
    const next = target + delta;
    run(entity, 'set_temperature', { temperature: next }, { temperature: next });
  };

  return (
    <div className="min-w-0 flex items-center gap-2.5 rounded-xl border border-ink/30 bg-surface-raised/40 px-3 py-2.5">
      <span className="p-1.5 rounded-lg shrink-0 bg-info/15 text-info">
        <Thermometer size={18} />
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-content-primary truncate">{entity.friendly_name}</p>
        <p aria-live="polite" className={`text-xs ${error ? 'text-danger' : 'text-content-muted'}`}>
          {error ||
            [
              typeof entity.current_temperature === 'number' ? `Now ${entity.current_temperature}°` : null,
              entity.state !== 'off' ? entity.state.replace('_', ' ') : 'Off',
            ]
              .filter(Boolean)
              .join(' · ')}
        </p>
      </div>
      {typeof target === 'number' && (
        <div className="flex items-center gap-1.5">
          <Button icon variant="secondary" className="min-h-[44px] min-w-[44px]" disabled={busy} onClick={() => nudge(-1)} aria-label={`Lower ${entity.friendly_name} target`}>
            <Minus size={14} />
          </Button>
          <span className="w-10 text-center text-sm font-semibold text-content-primary tabular-nums">{target}°</span>
          <Button icon variant="secondary" className="min-h-[44px] min-w-[44px]" disabled={busy} onClick={() => nudge(1)} aria-label={`Raise ${entity.friendly_name} target`}>
            <Plus size={14} />
          </Button>
        </div>
      )}
    </div>
  );
}
