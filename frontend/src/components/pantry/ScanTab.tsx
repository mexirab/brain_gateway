'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Camera, ImagePlus, Loader2, RotateCw, X } from 'lucide-react';
import { Badge, Button, Card } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import { resizeAll } from '@/lib/image-resize';
import {
  CATEGORIES,
  LEVELS,
  LOCATIONS,
  pantryApi,
  type Category,
  type Level,
  type PantryLocation,
  type PantryScan,
  type ScanLine,
  type TrackMode,
} from '@/lib/pantry-api';

const MAX_PHOTOS = 8;
const POLL_MS = 3000;
const MAX_POLL_MS = 30000;
// Matches the backend's STUCK_SCAN_MINUTES: after this a reading scan can be discarded.
const STUCK_MS = 10 * 60 * 1000;
const LABEL: Record<PantryLocation, string> = { fridge: 'Fridge', freezer: 'Freezer', pantry: 'Pantry' };
const OPEN_STATUSES = ['queued', 'processing', 'pending_review', 'failed'];

interface Staged {
  id: string;
  file: File;
  url: string;
}

export default function ScanTab({ onCommitted }: { onCommitted: (msg: string) => void }) {
  const [kind, setKind] = useState<PantryLocation>('fridge');
  const [staged, setStaged] = useState<Staged[]>([]);
  const [uploading, setUploading] = useState(false);
  const [open, setOpen] = useState<PantryScan[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  // Review edits per scan, so switching between open scans never loses them.
  const [drafts, setDrafts] = useState<Record<number, ScanLine[]>>({});
  const [error, setError] = useState<string | null>(null);

  const loadOpen = useCallback(async () => {
    try {
      const scans = await pantryApi.scans();
      const live = scans.filter((s) => OPEN_STATUSES.includes(s.status));
      setOpen(live);
      setActiveId((cur) => (cur !== null && live.some((s) => s.id === cur) ? cur : (live[0]?.id ?? null)));
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t load your scans.'));
    }
  }, []);

  useEffect(() => {
    loadOpen();
  }, [loadOpen]);

  // Revoke preview URLs on unmount.
  const stagedRef = useRef(staged);
  stagedRef.current = staged;
  useEffect(() => () => stagedRef.current.forEach((s) => URL.revokeObjectURL(s.url)), []);

  const addFiles = (files: FileList | null) => {
    if (!files) return;
    const next = Array.from(files)
      .filter((f) => f.type.startsWith('image/') || f.type === '')
      .slice(0, MAX_PHOTOS - staged.length)
      .map((file) => ({ id: `${file.name}-${file.size}-${Math.random()}`, file, url: URL.createObjectURL(file) }));
    setStaged((s) => [...s, ...next]);
  };

  const unstage = (id: string) =>
    setStaged((s) => {
      const hit = s.find((x) => x.id === id);
      if (hit) URL.revokeObjectURL(hit.url);
      return s.filter((x) => x.id !== id);
    });

  const submit = async () => {
    if (staged.length === 0) return;
    setUploading(true);
    setError(null);
    let blobs: Blob[];
    try {
      blobs = await resizeAll(staged.map((s) => s.file));
    } catch {
      setError('One of those photos couldn’t be opened on this device. Try retaking it, or use JPEG.');
      setUploading(false);
      return;
    }
    try {
      const res = await pantryApi.createScan(kind, blobs);
      staged.forEach((s) => URL.revokeObjectURL(s.url));
      setStaged([]);
      setActiveId(res.scan_id);
      await loadOpen();
    } catch (e) {
      setError(friendlyError(e, 'Couldn’t upload those photos.', { preferDetail: true }));
    } finally {
      setUploading(false);
    }
  };

  const finish = (scanId: number, msg: string | null) => {
    setDrafts(({ [scanId]: _gone, ...rest }) => rest);
    setActiveId(null);
    if (msg) onCommitted(msg);
    loadOpen();
  };

  return (
    <div className="space-y-5">
      {error && (
        <Card padding="none" className="p-3 text-sm text-danger/80 border-danger/30 flex items-start gap-2">
          <span className="flex-1">{error}</span>
          <Button icon size="sm" variant="ghost" onClick={() => setError(null)} aria-label="Dismiss error">
            <X size={16} />
          </Button>
        </Card>
      )}

      {open.length > 1 && (
        <div className="flex flex-wrap gap-2" aria-label="Open scans">
          {open.map((s) => (
            <button
              key={s.id}
              onClick={() => setActiveId(s.id)}
              aria-pressed={activeId === s.id}
              className={`px-3 py-2 rounded-full text-xs border ${
                activeId === s.id ? 'bg-brand/20 border-brand/40 font-medium' : 'border-line-subtle text-content-muted'
              }`}
            >
              {LABEL[s.kind]} · {new Date(s.created_at).toLocaleDateString([], { weekday: 'short' })} ·{' '}
              {s.status.replace('_', ' ')}
            </button>
          ))}
        </div>
      )}

      {activeId !== null && (
        <ActiveScan
          key={activeId}
          scanId={activeId}
          lines={drafts[activeId]}
          setLines={(fn) => setDrafts((d) => ({ ...d, [activeId]: fn(d[activeId] ?? []) }))}
          onStatusChange={loadOpen}
          onDone={(msg) => finish(activeId, msg)}
        />
      )}

      <Card padding="sm" className="space-y-4">
        <div>
          <h2 className="text-sm font-medium text-content-primary">New photo stock-take</h2>
          <p className="text-xs text-content-muted mt-1">
            One photo per shelf or drawer, up to {MAX_PHOTOS}. Jess lists what she can see, then you tick off what’s
            right — nothing is saved until you confirm.
          </p>
        </div>
        <div className="flex gap-2" aria-label="Where are the photos from">
          {LOCATIONS.map((l) => (
            <button
              key={l}
              aria-pressed={kind === l}
              onClick={() => setKind(l)}
              className={`flex-1 py-2.5 rounded-lg text-sm border ${
                kind === l
                  ? 'bg-brand/20 border-brand/40 text-content-primary font-medium'
                  : 'border-line-subtle text-content-muted'
              }`}
            >
              {LABEL[l]}
            </button>
          ))}
        </div>
        <div className="grid grid-cols-2 gap-2">
          <PickButton
            icon={<Camera size={16} />}
            label="Take photo"
            capture
            disabled={staged.length >= MAX_PHOTOS}
            onFiles={addFiles}
          />
          <PickButton
            icon={<ImagePlus size={16} />}
            label="Choose photos"
            multiple
            disabled={staged.length >= MAX_PHOTOS}
            onFiles={addFiles}
          />
        </div>
        {staged.length > 0 && (
          <div className="grid grid-cols-4 gap-2">
            {staged.map((s, i) => (
              <div key={s.id} className="relative aspect-square">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={s.url} alt={`Staged photo ${i + 1}`} className="w-full h-full object-cover rounded-md" />
                <button
                  onClick={() => unstage(s.id)}
                  className="absolute top-0 right-0 p-2"
                  aria-label={`Remove photo ${i + 1}`}
                >
                  <span className="block bg-black/70 rounded-full p-1 text-white">
                    <X size={14} />
                  </span>
                </button>
              </div>
            ))}
          </div>
        )}
        <Button variant="primary" className="w-full" disabled={staged.length === 0 || uploading} onClick={submit}>
          {uploading
            ? 'Uploading…'
            : staged.length === 0
              ? 'Add photos to start'
              : `Read ${staged.length} photo${staged.length > 1 ? 's' : ''}`}
        </Button>
      </Card>
    </div>
  );
}

function PickButton({
  icon,
  label,
  capture,
  multiple,
  disabled,
  onFiles,
}: {
  icon: React.ReactNode;
  label: string;
  capture?: boolean;
  multiple?: boolean;
  disabled?: boolean;
  onFiles: (files: FileList | null) => void;
}) {
  return (
    <label
      className={`flex items-center justify-center gap-2 px-3 py-3 border rounded-lg text-sm focus-within:ring-2 focus-within:ring-brand ${
        disabled
          ? 'opacity-50 cursor-not-allowed border-line-subtle'
          : 'cursor-pointer bg-info/15 text-info border-info/30 hover:bg-info/25'
      }`}
    >
      {icon}
      {label}
      <input
        type="file"
        accept="image/*"
        className="sr-only"
        disabled={disabled}
        multiple={multiple}
        {...(capture ? { capture: 'environment' as const } : {})}
        onChange={(e) => {
          onFiles(e.target.files);
          e.target.value = '';
        }}
      />
    </label>
  );
}

function ActiveScan({
  scanId,
  lines: draft,
  setLines,
  onStatusChange,
  onDone,
}: {
  scanId: number;
  lines: ScanLine[] | undefined;
  setLines: (fn: (cur: ScanLine[]) => ScanLine[]) => void;
  onStatusChange: () => void;
  onDone: (msg: string | null) => void;
}) {
  const [scan, setScan] = useState<PantryScan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState(false);
  // Bumped after Retry to restart the polling loop below.
  const [pollKey, setPollKey] = useState(0);
  const lines = draft ?? [];
  const lastStatus = useRef<string | null>(null);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;
    let delay = POLL_MS;
    const tick = async () => {
      try {
        const s = await pantryApi.scan(scanId);
        if (cancelled) return;
        setScan(s);
        setPollError(false);
        delay = POLL_MS;
        if (s.status === 'pending_review' && s.proposed && draft === undefined) {
          const proposed = s.proposed.items;
          setLines((cur) => (cur.length ? cur : proposed));
        }
        if (lastStatus.current !== null && lastStatus.current !== s.status) onStatusChange();
        lastStatus.current = s.status;
        if (s.status !== 'queued' && s.status !== 'processing') return;
      } catch {
        if (cancelled) return;
        // One wifi drop must not freeze the screen: keep polling, slower.
        setPollError(true);
        delay = Math.min(delay * 2, MAX_POLL_MS);
      }
      timer = setTimeout(tick, delay);
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // draft/setLines/onStatusChange intentionally omitted: polling restarts
    // only on scan change or Retry, not on every review edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanId, pollKey]);

  const patch = (key: number, fields: Partial<ScanLine>) =>
    setLines((ls) => ls.map((l) => (l.key === key ? { ...l, ...fields } : l)));

  const act = async (fn: () => Promise<unknown>, after: (r: unknown) => void) => {
    setBusy(true);
    setError(null);
    try {
      after(await fn());
    } catch (e) {
      setError(friendlyError(e, 'That didn’t work — try again.', { preferDetail: true }));
    } finally {
      setBusy(false);
    }
  };

  const discard = () =>
    act(
      () => pantryApi.discardScan(scanId),
      () => onDone(null),
    );

  const status = (
    <p className="text-xs text-warning" aria-live="polite">
      {pollError ? 'Connection hiccup — still trying…' : ''}
    </p>
  );

  if (!scan) {
    return (
      <Card padding="sm" className="space-y-2">
        <div className="h-10 animate-pulse rounded bg-surface-raised/40" />
        {status}
      </Card>
    );
  }

  if (scan.status === 'queued' || scan.status === 'processing') {
    const stuck = Date.now() - new Date(scan.created_at).getTime() > STUCK_MS;
    return (
      <Card padding="sm" className="space-y-3">
        <div className="flex items-center gap-3">
          <Loader2 size={18} className="animate-spin text-info shrink-0" />
          <div>
            <div className="text-sm text-content-primary" aria-live="polite">
              Reading {scan.photo_count} {LABEL[scan.kind].toLowerCase()} photo{scan.photo_count > 1 ? 's' : ''}…
            </div>
            <div className="text-xs text-content-muted">
              About 5–10 seconds a photo. You can leave this page — the scan waits here for you.
            </div>
          </div>
        </div>
        {status}
        {stuck && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-content-muted flex-1">This is taking much longer than it should.</span>
            <Button size="sm" variant="ghost" disabled={busy} onClick={discard}>
              Discard
            </Button>
          </div>
        )}
        {error && <p className="text-xs text-danger">{error}</p>}
      </Card>
    );
  }

  if (scan.status === 'failed') {
    return (
      <Card padding="sm" className="space-y-3 border-danger/30">
        <div className="text-sm text-danger">Couldn’t read those photos.</div>
        {scan.error && <div className="text-xs text-content-muted">{scan.error}</div>}
        {error && <div className="text-xs text-danger">{error}</div>}
        <div className="flex gap-2">
          <Button
            variant="primary"
            disabled={busy}
            onClick={() =>
              act(
                () => pantryApi.retryScan(scanId),
                () => setPollKey((k) => k + 1),
              )
            }
          >
            <RotateCw size={14} /> Retry
          </Button>
          <Button variant="ghost" disabled={busy} onClick={discard}>
            Discard
          </Button>
        </div>
      </Card>
    );
  }

  if (scan.status !== 'pending_review') return null;

  const toSave = lines.filter((l) => l.include && l.name.trim());
  const missingCount = toSave.some((l) => l.track_mode === 'count' && (l.qty === null || Number.isNaN(l.qty)));
  const warnings = scan.proposed?.warnings ?? [];

  return (
    <Card padding="sm" className="space-y-4 border-info/30">
      <div>
        <h2 className="text-sm font-medium text-content-primary">
          Check the {LABEL[scan.kind].toLowerCase()} list ({lines.length} found)
        </h2>
        <p className="text-xs text-content-muted mt-1">
          Untick anything wrong, fix names and amounts. “Update” means it’s already in your{' '}
          {LABEL[scan.kind].toLowerCase()} and will be set to this amount. Unsure guesses start unticked.
        </p>
      </div>

      {scan.photos_available && scan.photo_count > 0 && (
        <div className="flex gap-2 overflow-x-auto pb-1">
          {Array.from({ length: scan.photo_count }, (_, i) => (
            <a key={i} href={pantryApi.scanPhotoUrl(scanId, i)} target="_blank" rel="noreferrer" className="shrink-0">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={pantryApi.scanPhotoUrl(scanId, i)}
                alt={`Shelf photo ${i + 1}`}
                className="h-16 w-16 object-cover rounded-md"
              />
            </a>
          ))}
        </div>
      )}

      {warnings.length > 0 && (
        <ul className="text-xs text-warning space-y-0.5">
          {warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}

      {lines.length === 0 && (
        <p className="text-sm text-content-muted">Jess couldn’t make out any food in these photos.</p>
      )}

      <ul className="space-y-2">
        {lines.map((l) => (
          <ReviewLine key={l.key} line={l} onChange={(f) => patch(l.key, f)} />
        ))}
      </ul>

      <Button
        variant="ghost"
        size="sm"
        onClick={() =>
          setLines((ls) => [
            ...ls,
            {
              key: Math.max(-1, ...ls.map((x) => x.key)) + 1,
              name: '',
              category: 'other',
              location: scan.kind,
              track_mode: 'level',
              qty: null,
              level: 'full',
              confidence: 'high',
              action: 'add',
              match_id: null,
              match_name: null,
              include: true,
            },
          ])
        }
      >
        + Add something Jess missed
      </Button>

      {missingCount && <p className="text-xs text-warning">Give a number for every ticked “count” item.</p>}
      {error && <p className="text-sm text-danger">{error}</p>}
      <div className="flex gap-2">
        <Button
          variant="primary"
          className="flex-1"
          disabled={busy || missingCount}
          onClick={() =>
            act(
              () =>
                pantryApi.commitScan(
                  scanId,
                  lines.filter((l) => l.name.trim()),
                ),
              (r) => {
                const res = r as { added: number; updated: number; skipped: number };
                const unticked = lines.filter((l) => !l.include || !l.name.trim()).length;
                const failed = Math.max(0, res.skipped - unticked);
                onDone(
                  `Saved: ${res.added} added, ${res.updated} updated` +
                    (failed ? `, ${failed} couldn’t be saved` : '') +
                    '.',
                );
              },
            )
          }
        >
          {busy ? 'Saving…' : `Save ${toSave.length} item${toSave.length === 1 ? '' : 's'}`}
        </Button>
        <Button variant="ghost" disabled={busy} onClick={discard}>
          Discard
        </Button>
      </div>
    </Card>
  );
}

function ReviewLine({ line, onChange }: { line: ScanLine; onChange: (f: Partial<ScanLine>) => void }) {
  const setMode = (mode: TrackMode) =>
    onChange({
      track_mode: mode,
      qty: mode === 'count' ? (line.qty ?? 1) : null,
      level: mode === 'count' ? null : (line.level ?? 'full'),
    });

  return (
    <li
      className={`rounded-lg border p-2 space-y-2 ${line.include ? '' : 'opacity-60'} ${
        line.confidence === 'low' ? 'border-warning/40' : 'border-line-subtle'
      }`}
    >
      <div className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={line.include}
          onChange={(e) => onChange({ include: e.target.checked })}
          aria-label={`Include ${line.name || 'item'}`}
          className="h-5 w-5 shrink-0"
        />
        <input
          className="input flex-1 py-1.5 text-base md:text-sm"
          value={line.name}
          maxLength={80}
          placeholder="Item name"
          // A renamed line is no longer the item it was matched to.
          onChange={(e) => onChange({ name: e.target.value, match_id: null, match_name: null, action: 'add' })}
          aria-label="Item name"
        />
        {line.action === 'update' ? (
          <Badge tone="info" title={`Already tracked as ${line.match_name}`}>
            update
          </Badge>
        ) : (
          <Badge tone="success">new</Badge>
        )}
        {line.confidence === 'low' && <Badge tone="warning">unsure</Badge>}
      </div>
      {line.include && (
        <div className="flex flex-wrap gap-2 pl-7">
          <select
            className="input py-1.5 text-base md:text-xs w-auto"
            value={line.track_mode}
            onChange={(e) => setMode(e.target.value as TrackMode)}
            aria-label="Tracking"
          >
            <option value="count">count</option>
            <option value="level">level</option>
            <option value="staple">staple</option>
          </select>
          {line.track_mode === 'count' ? (
            <input
              type="number"
              min={0}
              inputMode="numeric"
              className="input py-1.5 text-base md:text-xs w-20"
              value={line.qty ?? ''}
              onChange={(e) => onChange({ qty: e.target.value === '' ? null : Number(e.target.value) })}
              aria-label="Count"
            />
          ) : line.track_mode === 'level' ? (
            <select
              className="input py-1.5 text-base md:text-xs w-auto"
              value={line.level ?? 'full'}
              onChange={(e) => onChange({ level: e.target.value as Level })}
              aria-label="How full"
            >
              {LEVELS.filter((l) => l !== 'out').map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          ) : null}
          <select
            className="input py-1.5 text-base md:text-xs w-auto"
            value={line.category}
            onChange={(e) => onChange({ category: e.target.value as Category })}
            aria-label="Category"
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>
      )}
    </li>
  );
}
