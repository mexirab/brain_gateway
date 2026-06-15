'use client';

import { useState } from 'react';
import { Lightbulb } from 'lucide-react';
import { api } from '@/lib/api';
import type { HAEntity } from '@/lib/types';

interface LightControlProps {
  entity: HAEntity;
  onStateChange: (entityId: string, newState: string) => void;
}

export default function LightControl({ entity, onStateChange }: LightControlProps) {
  const isOn = entity.state === 'on';
  const [brightness, setBrightness] = useState(128);
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

  const handleBrightness = async (value: number) => {
    setBrightness(value);
    try {
      await api.haCommand(entity.entity_id, 'turn_on', { brightness: value });
      onStateChange(entity.entity_id, 'on');
    } catch {
      // silently fail
    }
  };

  return (
    <div className="flex items-center gap-3 p-3 rounded-lg bg-surface-raised/40 border border-line/30">
      <button
        onClick={handleToggle}
        disabled={acting}
        className={`p-2 rounded-lg transition-colors shrink-0 ${
          isOn
            ? 'bg-warning/20 text-warning'
            : 'bg-surface-raised/60 text-content-muted hover:text-content-secondary'
        }`}
      >
        <Lightbulb size={18} />
      </button>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-white truncate">
          {entity.friendly_name}
        </p>
        {isOn && (
          <input
            type="range"
            min={1}
            max={255}
            value={brightness}
            onChange={(e) => handleBrightness(Number(e.target.value))}
            className="w-full h-1 mt-1.5 accent-warning bg-surface-overlay rounded-full appearance-none cursor-pointer"
          />
        )}
      </div>
      <span className={`text-xs ${isOn ? 'text-warning' : 'text-content-muted'}`}>
        {isOn ? 'ON' : 'OFF'}
      </span>
    </div>
  );
}
