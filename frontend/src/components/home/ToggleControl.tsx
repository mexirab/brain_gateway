'use client';

import { useState } from 'react';
import { Power } from 'lucide-react';
import { api } from '@/lib/api';
import type { HAEntity } from '@/lib/types';

interface ToggleControlProps {
  entity: HAEntity;
  onStateChange: (entityId: string, newState: string) => void;
}

export default function ToggleControl({ entity, onStateChange }: ToggleControlProps) {
  const isOn = entity.state === 'on';
  const [acting, setActing] = useState(false);

  const handleToggle = async () => {
    setActing(true);
    const service = isOn ? 'turn_off' : 'turn_on';
    try {
      await api.haCommand(entity.entity_id, service);
      onStateChange(entity.entity_id, isOn ? 'off' : 'on');
    } catch {
      // revert silently
    } finally {
      setActing(false);
    }
  };

  return (
    <div className="flex items-center gap-3 p-3 rounded-lg bg-zinc-800/40 border border-zinc-700/30">
      <button
        onClick={handleToggle}
        disabled={acting}
        className={`p-2 rounded-lg transition-colors shrink-0 ${
          isOn
            ? 'bg-emerald-500/20 text-emerald-400'
            : 'bg-zinc-800/60 text-zinc-500 hover:text-zinc-400'
        }`}
      >
        <Power size={18} />
      </button>
      <p className="text-sm font-medium text-white truncate flex-1">
        {entity.friendly_name}
      </p>
      <span className={`text-xs ${isOn ? 'text-emerald-400' : 'text-zinc-600'}`}>
        {isOn ? 'ON' : 'OFF'}
      </span>
    </div>
  );
}
