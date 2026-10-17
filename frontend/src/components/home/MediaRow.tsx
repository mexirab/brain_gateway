'use client';

import { useEffect, useRef, useState } from 'react';
import { Pause, Play, Power, Speaker } from 'lucide-react';
import type { HAEntity } from '@/lib/types';
import { Button } from '@/components/ui';
import { useHomeControls } from './HomeContext';

const COMMIT_DELAY_MS = 300;

/** A speaker / TV: play-pause, volume, power. */
export default function MediaRow({ entity }: { entity: HAEntity }) {
  const { run, errors, pending } = useHomeControls();
  const playing = entity.state === 'playing';
  // Play only makes sense with something loaded (an idle Cast has nothing queued).
  const canPlayPause = playing || entity.state === 'paused';
  const off = entity.state === 'off' || entity.state === 'standby';
  const [vol, setVol] = useState(() => Math.round((entity.volume ?? 0) * 100));
  const dragging = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!dragging.current && typeof entity.volume === 'number') setVol(Math.round(entity.volume * 100));
  }, [entity.volume]);

  // Flush (not drop) a pending volume change if the row unmounts mid-debounce.
  const commit = useRef<(() => void) | null>(null);
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
    commit.current?.();
  }, []);

  const onSlide = (value: number) => {
    dragging.current = true;
    setVol(value);
    if (timer.current) clearTimeout(timer.current);
    commit.current = () => {
      commit.current = null;
      dragging.current = false;
      run(entity, 'volume_set', { volume_level: value / 100 }, { volume: value / 100 });
    };
    timer.current = setTimeout(() => commit.current?.(), COMMIT_DELAY_MS);
  };

  const busy = pending[entity.entity_id];
  const error = errors[entity.entity_id];
  const status = error || (entity.media_title && !off ? entity.media_title : stateLabel(entity.state));

  return (
    <div className="min-w-0 rounded-xl border border-line/30 bg-surface-raised/40 px-3 py-2.5">
      <div className="flex items-center gap-2.5">
        <span className={`p-1.5 rounded-lg shrink-0 ${playing ? 'bg-brand/20 text-brand' : 'bg-surface-overlay/60 text-content-muted'}`}>
          <Speaker size={18} />
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-white truncate">{entity.friendly_name}</p>
          <p aria-live="polite" className={`text-xs truncate ${error ? 'text-danger' : 'text-content-muted'}`}>{status}</p>
        </div>
        {canPlayPause && (
          <Button
            icon
            variant="secondary"
            className="min-h-[44px] min-w-[44px]"
            disabled={busy}
            onClick={() =>
              run(entity, playing ? 'media_pause' : 'media_play', undefined, { state: playing ? 'paused' : 'playing' })
            }
            aria-label={playing ? `Pause ${entity.friendly_name}` : `Play ${entity.friendly_name}`}
          >
            {playing ? <Pause size={16} /> : <Play size={16} />}
          </Button>
        )}
        <Button
          icon
          variant={off ? 'secondary' : 'ghost'}
          className="min-h-[44px] min-w-[44px]"
          disabled={busy}
          onClick={() => run(entity, off ? 'turn_on' : 'turn_off', undefined, { state: off ? 'on' : 'off' })}
          aria-label={off ? `Turn on ${entity.friendly_name}` : `Turn off ${entity.friendly_name}`}
        >
          <Power size={16} />
        </Button>
      </div>
      {!off && typeof entity.volume === 'number' && (
        <input
          type="range"
          min={0}
          max={100}
          value={vol}
          onChange={(e) => onSlide(Number(e.target.value))}
          aria-label={`${entity.friendly_name} volume`}
          className="w-full h-6 mt-1 accent-brand cursor-pointer touch-pan-y"
        />
      )}
    </div>
  );
}

function stateLabel(state: string): string {
  if (state === 'playing') return 'Playing';
  if (state === 'paused') return 'Paused';
  if (state === 'idle') return 'Idle';
  if (state === 'off' || state === 'standby') return 'Off';
  if (state === 'on') return 'On';
  return state;
}
