'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Home, RefreshCw } from 'lucide-react';
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
  const [stale, setStale] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [mode, setMode] = useState<Mode>('rooms');
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const reconcileTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef(0);
  const fetching = useRef(false);
  // Bumped by every command: a poll that started before a command must not
  // overwrite that command's optimistic state when it lands.
  const cmdSeq = useRef(0);
  const mounted = useRef(true);
  const errorTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  useEffect(() => {
    setMode(readStored<Mode>(MODE_KEY, 'rooms'));
    setOpen(readStored<Record<string, boolean>>(OPEN_KEY, {}));
  }, []);

  const fetchEntities = useCallback((quiet = false) => {
    // Background polls never overlap (a slow HA would otherwise stack them up).
    if (quiet && fetching.current) return Promise.resolve();
    fetching.current = true;
    const seq = cmdSeq.current;
    if (!quiet) setLoading(true);
    return api
      .entities()
      .then((data) => {
        if (!mounted.current) return;
        // Don't overwrite optimistic state from a command sent after this fetch began.
        if (inFlight.current === 0 && seq === cmdSeq.current) setControllable(data.controllable);
        setRoomsAvailable(data.rooms_available !== false);
        setStale(data.state_ok === false);
        setError(false);
      })
      .catch(() => {
        if (!mounted.current) return;
        // Once devices are on screen, any failed refresh shows the stale banner.
        setStale(true);
        if (!quiet) setError(true);
      })
      .finally(() => {
        fetching.current = false;
        if (!quiet && mounted.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    mounted.current = true;
    const timers = errorTimers.current;
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
      mounted.current = false;
      if (reconcileTimer.current) clearTimeout(reconcileTimer.current);
      Object.values(timers).forEach(clearTimeout);
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
    cmdSeq.current += 1;
    try {
      await api.haCommand(id, service, data);
      return true;
    } catch (err) {
      if (!mounted.current) return false;
      if (patch) {
        // Roll back only the fields we changed. Overlapping slider commits can
        // roll back to an older value; the reconcile fetch corrects it.
        const revert = Object.fromEntries(
          Object.keys(patch).map((k) => [k, entity[k as keyof HAEntity]]),
        ) as Partial<HAEntity>;
        patchEntity(id, revert);
      }
      setErrors((e) => ({ ...e, [id]: commandErrorMessage(err) }));
      clearTimeout(errorTimers.current[id]);
      errorTimers.current[id] = setTimeout(() => {
        delete errorTimers.current[id];
        setErrors(({ [id]: _drop, ...rest }) => rest);
      }, ERROR_MS);
      return false;
    } finally {
      inFlight.current -= 1;
      if (mounted.current) {
        setPending(({ [id]: _drop, ...rest }) => rest);
        // Pick up what HA actually did (groups, scenes changing other lights).
        if (reconcileTimer.current) clearTimeout(reconcileTimer.current);
        reconcileTimer.current = setTimeout(() => fetchEntities(true), RECONCILE_MS);
      }
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
    const next = { ...open, [sectionKey(name)]: !open[sectionKey(name)] };
    setOpen(next);
    writeStored(OPEN_KEY, next);
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
            className="min-h-[44px] min-w-[44px]"
          >
            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
          </Button>
        </div>

        {stale && hasAny && (
          <div role="status" className="mb-4 flex items-center gap-3 rounded-xl border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-warning">
            <AlertTriangle size={16} className="shrink-0" />
            <span className="flex-1">Can’t reach Home Assistant right now — showing the last known state.</span>
            <Button size="sm" variant="secondary" className="min-h-[44px]" onClick={() => fetchEntities()}>
              Retry
            </Button>
          </div>
        )}

        {roomsAvailable && hasAny && (
          <div role="group" aria-label="Group by" className="inline-flex p-1 mb-4 rounded-xl bg-surface-raised/50 border border-line/30">
            {(['rooms', 'type'] as const).map((m) => (
              <button
                key={m}
                type="button"
                aria-pressed={mode === m}
                onClick={() => changeMode(m)}
                className={`min-h-[44px] px-5 rounded-lg text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50 ${
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
