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
    <div className="flex items-center gap-3 p-3 rounded-lg bg-surface-raised/40 border border-line/30">
      <button
        onClick={handleToggle}
        disabled={acting}
        className={`p-2 rounded-lg transition-colors shrink-0 ${
          isOn
            ? 'bg-success/20 text-success'
            : 'bg-surface-raised/60 text-content-muted hover:text-content-secondary'
        }`}
      >
        <Power size={18} />
      </button>
      <p className="text-sm font-medium text-white truncate flex-1">
        {entity.friendly_name}
      </p>
      <span className={`text-xs ${isOn ? 'text-success' : 'text-content-muted'}`}>
        {isOn ? 'ON' : 'OFF'}
      </span>
    </div>
  );
}
