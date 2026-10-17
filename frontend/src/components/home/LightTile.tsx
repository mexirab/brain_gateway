'use client';

import { useEffect, useRef, useState } from 'react';
import { Lightbulb, Layers } from 'lucide-react';
import type { HAEntity } from '@/lib/types';
import { brightnessPct, stripRoom } from '@/lib/home-groups';
import { useHomeControls } from './HomeContext';

const COMMIT_DELAY_MS = 300;

/** One light: tap the tile to toggle; a brightness slider shows while on. */
export default function LightTile({ entity, roomName }: { entity: HAEntity; roomName?: string }) {
  const { run, errors, pending } = useHomeControls();
  const isOn = entity.state === 'on';
  const [pct, setPct] = useState(() => brightnessPct(entity.brightness));
  const dragging = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Follow server state unless the user is mid-drag.
  useEffect(() => {
    if (!dragging.current) setPct(brightnessPct(entity.brightness));
  }, [entity.brightness]);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const onSlide = (value: number) => {
    dragging.current = true;
    setPct(value);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      dragging.current = false;
      run(entity, 'turn_on', { brightness_pct: value }, { state: 'on', brightness: Math.round((value / 100) * 255) });
    }, COMMIT_DELAY_MS);
  };

  const toggle = () =>
    run(entity, isOn ? 'turn_off' : 'turn_on', undefined, { state: isOn ? 'off' : 'on' });

  const error = errors[entity.entity_id];
  const Icon = entity.is_group ? Layers : Lightbulb;

  return (
    <div
      className={`min-w-0 rounded-xl border transition-colors ${
        isOn ? 'bg-warning/10 border-warning/30' : 'bg-surface-raised/40 border-line/30'
      }`}
    >
      <button
        type="button"
        onClick={toggle}
        disabled={pending[entity.entity_id]}
        aria-pressed={isOn}
        className="flex items-center gap-2.5 w-full min-h-[52px] px-3 py-2.5 text-left disabled:opacity-60"
      >
        <span
          className={`p-1.5 rounded-lg shrink-0 ${isOn ? 'bg-warning/20 text-warning' : 'bg-surface-overlay/60 text-content-muted'}`}
        >
          <Icon size={18} />
        </span>
        <span className="flex-1 min-w-0">
          <span className="block text-sm font-medium text-white truncate" title={entity.friendly_name}>
            {stripRoom(entity.friendly_name, roomName)}
          </span>
          <span className={`block text-xs ${error ? 'text-danger' : isOn ? 'text-warning' : 'text-content-muted'}`}>
            {error || (isOn ? (entity.supports_brightness ? `On · ${pct}%` : 'On') : 'Off')}
          </span>
        </span>
      </button>
      {isOn && entity.supports_brightness && (
        <div className="px-3 pb-3 -mt-1">
          <input
            type="range"
            min={1}
            max={100}
            value={pct}
            onChange={(e) => onSlide(Number(e.target.value))}
            aria-label={`${entity.friendly_name} brightness`}
            className="w-full h-6 accent-warning cursor-pointer touch-pan-y"
          />
        </div>
      )}
    </div>
  );
}
