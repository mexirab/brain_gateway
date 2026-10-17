'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Home, RefreshCw } from 'lucide-react';
import { api } from '@/lib/api';
import type { HAEntity } from '@/lib/types';
import { buildLayout, commandErrorMessage } from '@/lib/home-groups';
import { HomeControlsContext, type HomeControls } from '@/components/home/HomeContext';
import SectionCard from '@/components/home/SectionCard';
import CollapsedList from '@/components/home/CollapsedList';
import ToggleRow from '@/components/home/ToggleRow';
import { Card, Button, ErrorState, EmptyState, Skeleton } from '@/components/ui';

type Mode = 'rooms' | 'type';

const POLL_MS = 20_000;
const RECONCILE_MS = 1_500;
const ERROR_MS = 5_000;
const MODE_KEY = 'home.mode';
const OPEN_KEY = 'home.open';

function readStored<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeStored(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // private mode / storage blocked — preference just isn't remembered
  }
}

export default function HomePage() {
  const [controllable, setControllable] = useState<Record<string, HAEntity[]>>({});
  const [roomsAvailable, setRoomsAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [mode, setMode] = useState<Mode>('rooms');
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const reconcileTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef(0);

  useEffect(() => {
    setMode(readStored<Mode>(MODE_KEY, 'rooms'));
    setOpen(readStored<Record<string, boolean>>(OPEN_KEY, {}));
  }, []);

  const fetchEntities = useCallback((quiet = false) => {
    if (!quiet) setLoading(true);
    return api
      .entities()
      .then((data) => {
        // Don't overwrite optimistic state while a command is still in flight.
        if (inFlight.current === 0) setControllable(data.controllable);
        setRoomsAvailable(data.rooms_available !== false);
        setError(false);
      })
      .catch(() => {
        if (!quiet) setError(true);
      })
      .finally(() => {
        if (!quiet) setLoading(false);
      });
  }, []);

  useEffect(() => {
    fetchEntities();
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') fetchEntities(true);
    }, POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible') fetchEntities(true);
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
      if (reconcileTimer.current) clearTimeout(reconcileTimer.current);
    };
  }, [fetchEntities]);

  const patchEntity = (entityId: string, patch: Partial<HAEntity>) =>
    setControllable((prev) => {
      const next: Record<string, HAEntity[]> = {};
      for (const [domain, list] of Object.entries(prev)) {
        next[domain] = list.map((e) => (e.entity_id === entityId ? { ...e, ...patch } : e));
      }
      return next;
    });

  const run = useCallback<HomeControls['run']>(async (entity, service, data, patch) => {
    const id = entity.entity_id;
    setErrors(({ [id]: _drop, ...rest }) => rest);
    setPending((p) => ({ ...p, [id]: true }));
    if (patch) patchEntity(id, patch);
    inFlight.current += 1;
    try {
      await api.haCommand(id, service, data);
      return true;
    } catch (err) {
      if (patch) {
        // Roll back only the fields we changed.
        const revert = Object.fromEntries(
          Object.keys(patch).map((k) => [k, entity[k as keyof HAEntity]]),
        ) as Partial<HAEntity>;
        patchEntity(id, revert);
      }
      setErrors((e) => ({ ...e, [id]: commandErrorMessage(err) }));
      setTimeout(() => setErrors(({ [id]: _drop, ...rest }) => rest), ERROR_MS);
      return false;
    } finally {
      inFlight.current -= 1;
      setPending(({ [id]: _drop, ...rest }) => rest);
      // Pick up what HA actually did (groups, scenes changing other lights).
      if (reconcileTimer.current) clearTimeout(reconcileTimer.current);
      reconcileTimer.current = setTimeout(() => fetchEntities(true), RECONCILE_MS);
    }
  }, [fetchEntities]);

  const controls = useMemo<HomeControls>(() => ({ run, errors, pending }), [run, errors, pending]);

  const effectiveMode: Mode = roomsAvailable ? mode : 'type';
  const layout = useMemo(() => buildLayout(controllable, effectiveMode), [controllable, effectiveMode]);
  const lightsOnCount = useMemo(
    () => (controllable.light || []).filter((l) => l.state === 'on' && !l.is_group).length,
    [controllable],
  );

  const changeMode = (m: Mode) => {
    setMode(m);
    writeStored(MODE_KEY, m);
  };

  const sectionKey = (name: string) => `${effectiveMode}:${name}`;
  const toggleOpen = (name: string) => {
    setOpen((prev) => {
      const next = { ...prev, [sectionKey(name)]: !prev[sectionKey(name)] };
      writeStored(OPEN_KEY, next);
      return next;
    });
  };

  const hasAny = layout.sections.length + layout.settings.length + layout.unavailable.length > 0;

  return (
    <HomeControlsContext.Provider value={controls}>
      <div className="max-w-4xl">
        <div className="flex items-center justify-between gap-3 mb-4">
          <div className="min-w-0">
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <Home size={24} className="text-brand" />
              Home
            </h1>
            {hasAny && (
              <p className="text-xs text-content-muted mt-0.5">
                {lightsOnCount === 0 ? 'All lights off' : `${lightsOnCount} light${lightsOnCount === 1 ? '' : 's'} on`}
              </p>
            )}
          </div>
          <Button
            icon
            variant="secondary"
            onClick={() => fetchEntities()}
            disabled={loading}
            aria-label="Refresh"
          >
            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
          </Button>
        </div>

        {roomsAvailable && hasAny && (
          <div role="tablist" aria-label="Group by" className="inline-flex p-1 mb-4 rounded-xl bg-surface-raised/50 border border-line/30">
            {(['rooms', 'type'] as const).map((m) => (
              <button
                key={m}
                role="tab"
                type="button"
                aria-selected={mode === m}
                onClick={() => changeMode(m)}
                className={`min-h-[36px] px-4 rounded-lg text-sm transition-colors ${
                  mode === m ? 'bg-brand/20 text-white font-medium' : 'text-content-muted hover:text-white'
                }`}
              >
                {m === 'rooms' ? 'Rooms' : 'Type'}
              </button>
            ))}
          </div>
        )}

        {loading && !hasAny && !error && (
          <div className="space-y-3">
            {[1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-14" />
            ))}
          </div>
        )}

        {error && !hasAny && (
          <Card padding="lg">
            <ErrorState
              title="Could not reach Home Assistant"
              message="We couldn’t load your devices. Check that Home Assistant is reachable, then try again."
              onRetry={() => fetchEntities()}
            />
          </Card>
        )}

        {hasAny && (
          <div className="space-y-3">
            {layout.sections.map((section) => (
              <SectionCard
                key={sectionKey(section.name)}
                section={section}
                isRoom={effectiveMode === 'rooms'}
                open={!!open[sectionKey(section.name)]}
                onToggleOpen={() => toggleOpen(section.name)}
              />
            ))}

            <CollapsedList
              title="Device settings"
              hint="Configuration toggles on your devices (camera enabled, sensor enabled, LEDs…)."
              count={layout.settings.length}
            >
              <div className="grid gap-2 sm:grid-cols-2">
                {layout.settings.map((e) => (
                  <ToggleRow key={e.entity_id} entity={e} />
                ))}
              </div>
            </CollapsedList>

            <CollapsedList title="Unavailable" hint="Offline or not reporting right now." count={layout.unavailable.length}>
              {layout.unavailable.map((e) => (
                <div key={e.entity_id} className="flex items-center justify-between text-sm px-1 py-1.5">
                  <span className="text-content-secondary truncate">{e.friendly_name}</span>
                  <span className="text-xs text-content-muted shrink-0 ml-2">{e.area || e.state}</span>
                </div>
              ))}
            </CollapsedList>
          </div>
        )}

        {!loading && !error && !hasAny && (
          <Card padding="lg">
            <EmptyState
              icon={<Home size={40} />}
              title="No controllable devices found"
              description="Once Home Assistant exposes devices, they’ll show up here."
            />
          </Card>
        )}
      </div>
    </HomeControlsContext.Provider>
  );
}
