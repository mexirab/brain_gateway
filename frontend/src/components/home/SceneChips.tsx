'use client';

import { useState } from 'react';
import { Check, Sparkles } from 'lucide-react';
import type { HAEntity } from '@/lib/types';
import { stripRoom } from '@/lib/home-groups';
import { useHomeControls } from './HomeContext';

const VISIBLE = 6;

/** Scenes as one-tap buttons; long lists show the first few plus "N more". */
export default function SceneChips({ scenes, roomName }: { scenes: HAEntity[]; roomName?: string }) {
  const { run, errors, pending } = useHomeControls();
  const [expanded, setExpanded] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  const shown = expanded ? scenes : scenes.slice(0, VISIBLE);

  const activate = async (scene: HAEntity) => {
    if (await run(scene, 'turn_on')) {
      setDone(scene.entity_id);
      setTimeout(() => setDone((d) => (d === scene.entity_id ? null : d)), 2000);
    }
  };

  const label = (s: HAEntity) => stripRoom(s.friendly_name, roomName);

  return (
    <div className="flex flex-wrap gap-2">
      {shown.map((scene) => {
        const ok = done === scene.entity_id;
        const error = errors[scene.entity_id];
        return (
          <button
            key={scene.entity_id}
            type="button"
            onClick={() => activate(scene)}
            disabled={pending[scene.entity_id]}
            title={error || scene.friendly_name}
            className={`flex items-center gap-1.5 min-h-[40px] px-3 rounded-full text-sm border transition-colors disabled:opacity-60 ${
              error
                ? 'border-danger/50 text-danger bg-danger/10'
                : ok
                  ? 'border-brand/50 text-brand bg-brand/20'
                  : 'border-line/40 text-content-secondary bg-surface-raised/40 hover:text-white'
            }`}
          >
            {ok ? <Check size={14} /> : <Sparkles size={14} />}
            {label(scene)}
          </button>
        );
      })}
      {scenes.length > VISIBLE && (
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="min-h-[40px] px-3 rounded-full text-sm text-content-muted hover:text-white"
        >
          {expanded ? 'Show fewer' : `${scenes.length - VISIBLE} more`}
        </button>
      )}
    </div>
  );
}
