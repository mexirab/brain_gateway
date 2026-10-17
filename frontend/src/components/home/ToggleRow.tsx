'use client';

import { Power, Wind } from 'lucide-react';
import type { HAEntity } from '@/lib/types';
import { useHomeControls } from './HomeContext';

/** A switch or fan: the whole row is the on/off button. */
export default function ToggleRow({ entity }: { entity: HAEntity }) {
  const { run, errors, pending } = useHomeControls();
  const isOn = entity.state === 'on';
  const error = errors[entity.entity_id];
  const Icon = entity.entity_id.startsWith('fan.') ? Wind : Power;

  return (
    <button
      type="button"
      onClick={() => run(entity, isOn ? 'turn_off' : 'turn_on', undefined, { state: isOn ? 'off' : 'on' })}
      disabled={pending[entity.entity_id]}
      aria-pressed={isOn}
      className={`min-w-0 flex items-center gap-2.5 w-full min-h-[52px] px-3 py-2.5 rounded-xl border text-left transition-colors disabled:opacity-60 ${
        isOn ? 'bg-success/10 border-success/30' : 'bg-surface-raised/40 border-line/30'
      }`}
    >
      <span className={`p-1.5 rounded-lg shrink-0 ${isOn ? 'bg-success/20 text-success' : 'bg-surface-overlay/60 text-content-muted'}`}>
        <Icon size={18} />
      </span>
      <span className="flex-1 min-w-0 text-sm font-medium text-white truncate">{entity.friendly_name}</span>
      <span className={`text-xs ${error ? 'text-danger' : isOn ? 'text-success' : 'text-content-muted'}`}>
        {error || (isOn ? 'On' : 'Off')}
      </span>
    </button>
  );
}
