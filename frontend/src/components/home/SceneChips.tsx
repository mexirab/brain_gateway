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

  const failed = scenes.find((s) => errors[s.entity_id]);

  return (
    <div className="flex flex-wrap gap-2">
      <span className="sr-only" aria-live="polite">
        {failed ? `${failed.friendly_name}: ${errors[failed.entity_id]}` : ''}
      </span>
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
            className={`flex items-center gap-1.5 min-h-[44px] px-3.5 rounded-full text-sm border transition-colors disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50 ${
              error
                ? 'border-danger/50 text-danger bg-danger/10'
                : ok
                  ? 'border-brand/50 text-brand bg-brand/20'
                  : 'border-ink/40 text-content-secondary bg-surface-raised/40 hover:text-content-primary'
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
          className="min-h-[44px] px-3 rounded-full text-sm text-content-muted hover:text-content-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
        >
          {expanded ? 'Show fewer' : `${scenes.length - VISIBLE} more`}
        </button>
      )}
    </div>
  );
}
