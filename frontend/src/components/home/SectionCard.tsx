'use client';

import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { Section } from '@/lib/home-groups';
import { lightsOn, sectionSize } from '@/lib/home-groups';
import { Button, Card } from '@/components/ui';
import { useHomeControls } from './HomeContext';
import LightTile from './LightTile';
import SceneChips from './SceneChips';
import MediaRow from './MediaRow';
import ToggleRow from './ToggleRow';
import ClimateRow from './ClimateRow';

interface SectionCardProps {
  section: Section;
  open: boolean;
  onToggleOpen: () => void;
  /** Room view: strip the room name from scene labels, offer "Lights off". */
  isRoom: boolean;
}

/** One room (or one kind, in "by type" view): lights, scenes, media,
 *  switches, climate — in that order. */
export default function SectionCard({ section, open, onToggleOpen, isRoom }: SectionCardProps) {
  const { run, pending } = useHomeControls();
  const on = lightsOn(section);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(0);
  // Every light that is on, groups included (turning a group off twice is harmless).
  const toTurnOff = section.lights.filter((l) => l.state === 'on');
  const allOff = async () => {
    setBusy(true);
    setFailed(0);
    const results = await Promise.all(toTurnOff.map((l) => run(l, 'turn_off', undefined, { state: 'off' })));
    setBusy(false);
    const n = results.filter((ok) => !ok).length;
    setFailed(n);
    if (n) setTimeout(() => setFailed(0), 5000);
  };
  const anyPending = section.lights.some((l) => pending[l.entity_id]);

  const summary = [
    section.lights.length > 0 ? (on.length ? `${on.length} on` : 'all off') : null,
    section.media.some((m) => m.state === 'playing') ? 'playing' : null,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <Card padding="none" className="overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-3">
        <button
          type="button"
          onClick={onToggleOpen}
          aria-expanded={open}
          className="flex items-center gap-2 flex-1 min-w-0 min-h-[44px] text-left rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
        >
          {open ? <ChevronDown size={16} className="text-content-muted shrink-0" /> : <ChevronRight size={16} className="text-content-muted shrink-0" />}
          <span className="text-base font-semibold text-content-primary truncate">{section.name}</span>
          <span className="text-xs text-content-muted shrink-0">{summary || `${sectionSize(section)}`}</span>
        </button>
        {failed > 0 && (
          <span role="status" className="text-xs text-danger shrink-0">
            {failed} didn’t turn off
          </span>
        )}
        {isRoom && toTurnOff.length > 0 && (
          <Button size="sm" variant="secondary" className="min-h-[44px]" disabled={busy || anyPending} onClick={allOff}>
            Lights off
          </Button>
        )}
      </div>

      {open && (
        <div className="px-4 pb-4 space-y-3">
          {section.lights.length > 0 && (
            <div className="grid grid-cols-2 lg:grid-cols-3 gap-2 [&>*]:min-w-0">
              {section.lights.map((e) => (
                <LightTile key={e.entity_id} entity={e} roomName={isRoom ? section.name : undefined} />
              ))}
            </div>
          )}
          {section.scenes.length > 0 && <SceneChips scenes={section.scenes} roomName={isRoom ? section.name : undefined} />}
          {section.media.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 [&>*]:min-w-0">
              {section.media.map((e) => (
                <MediaRow key={e.entity_id} entity={e} />
              ))}
            </div>
          )}
          {section.switches.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 [&>*]:min-w-0">
              {section.switches.map((e) => (
                <ToggleRow key={e.entity_id} entity={e} />
              ))}
            </div>
          )}
          {/* Covers / locks: shown read-only — opening and unlocking aren't allowed from here. */}
          {section.other.map((e) => (
            <div key={e.entity_id} className="flex items-center justify-between rounded-xl border border-ink/30 bg-surface-raised/40 px-3 py-2.5">
              <span className="text-sm text-content-primary truncate">{e.friendly_name}</span>
              <span className="text-xs text-content-muted">{e.state}</span>
            </div>
          ))}
          {section.climate.map((e) => (
            <ClimateRow key={e.entity_id} entity={e} />
          ))}
        </div>
      )}
    </Card>
  );
}
