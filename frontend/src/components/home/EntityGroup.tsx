'use client';

import { useState } from 'react';
import { ChevronDown, ChevronRight, Lightbulb, Power, Wind, Thermometer, DoorOpen, Lock, Sparkles } from 'lucide-react';
import type { HAEntity } from '@/lib/types';
import LightControl from './LightControl';
import ToggleControl from './ToggleControl';
import SceneButton from './SceneButton';

const DOMAIN_ICONS: Record<string, React.ElementType> = {
  light: Lightbulb,
  switch: Power,
  fan: Wind,
  climate: Thermometer,
  cover: DoorOpen,
  lock: Lock,
  scene: Sparkles,
};

const DOMAIN_LABELS: Record<string, string> = {
  light: 'Lights',
  switch: 'Switches',
  fan: 'Fans',
  climate: 'Climate',
  cover: 'Covers',
  lock: 'Locks',
  scene: 'Scenes',
};

interface EntityGroupProps {
  domain: string;
  entities: HAEntity[];
  onStateChange: (entityId: string, newState: string) => void;
}

export default function EntityGroup({ domain, entities, onStateChange }: EntityGroupProps) {
  const [open, setOpen] = useState(true);
  const Icon = DOMAIN_ICONS[domain] || Power;
  const label = DOMAIN_LABELS[domain] || domain;

  const onCount = entities.filter((e) => e.state === 'on').length;

  return (
    <div className="glass p-4">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 w-full text-left mb-2"
      >
        {open ? <ChevronDown size={16} className="text-zinc-500" /> : <ChevronRight size={16} className="text-zinc-500" />}
        <Icon size={18} className="text-indigo-400" />
        <span className="text-sm font-semibold text-white flex-1">{label}</span>
        <span className="text-xs text-zinc-500">
          {entities.length} {onCount > 0 && `(${onCount} on)`}
        </span>
      </button>

      {open && (
        <div className={domain === 'scene' ? 'flex flex-wrap gap-2 mt-2' : 'space-y-2 mt-2'}>
          {entities.map((entity) => {
            if (domain === 'scene') {
              return <SceneButton key={entity.entity_id} entity={entity} />;
            }
            if (domain === 'light') {
              return (
                <LightControl
                  key={entity.entity_id}
                  entity={entity}
                  onStateChange={onStateChange}
                />
              );
            }
            return (
              <ToggleControl
                key={entity.entity_id}
                entity={entity}
                onStateChange={onStateChange}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
