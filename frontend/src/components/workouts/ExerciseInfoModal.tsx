'use client';

import { useCallback, useEffect, useState } from 'react';
import { ExternalLink, Pause, Play } from 'lucide-react';
import { api } from '@/lib/api';
import { ErrorState, Modal, Skeleton } from '@/components/ui';
import type { ExerciseInfo } from '@/lib/types';

const FRAME_MS = 1000;
const FOCUS_RING = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50';

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function videoSearchUrl(name: string): string {
  return `https://www.youtube.com/results?search_query=${encodeURIComponent(`${name} exercise form`)}`;
}

function VideoLink({ href }: { href: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={`inline-flex items-center gap-1.5 text-sm text-info hover:underline rounded ${FOCUS_RING}`}
    >
      <ExternalLink size={14} aria-hidden /> Watch a video
    </a>
  );
}

/**
 * "How do I do this?" card: flips the start/end photos like a two-frame
 * animation and lists the steps. Falls back to a video-search link when the
 * library has no faithful match (or hasn't been downloaded yet).
 */
export function ExerciseInfoModal({ name, onClose }: { name: string | null; onClose: () => void }) {
  // Keyed by name so reopening on a different exercise never paints stale data.
  const [loaded, setLoaded] = useState<{ name: string; info: ExerciseInfo } | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);
  const [frame, setFrame] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [imgBroken, setImgBroken] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!name) return;
    let cancelled = false;
    setFailedFor(null);
    setFrame(0);
    setImgBroken(false);
    setPlaying(!prefersReducedMotion());
    api
      .exerciseInfo(name)
      .then((info) => !cancelled && setLoaded({ name, info }))
      .catch(() => !cancelled && setFailedFor(name));
    return () => {
      cancelled = true;
    };
  }, [name, attempt]);

  const retry = useCallback(() => setAttempt((a) => a + 1), []);

  const info = name && loaded?.name === name ? loaded.info : null;
  const error = name !== null && failedFor === name;
  const frames = info?.frames ?? [];

  useEffect(() => {
    if (!name || !playing || frames.length < 2) return;
    const t = setInterval(() => setFrame((f) => (f + 1) % frames.length), FRAME_MS);
    return () => clearInterval(t);
  }, [name, playing, frames.length]);

  const pictured = info?.match ?? name ?? '';
  const altText =
    frames.length === 2
      ? `${pictured}: ${frame === 0 ? 'start' : 'end'} position`
      : `${pictured}: position ${frame + 1} of ${frames.length}`;

  return (
    <Modal open={name !== null} onClose={onClose} title={name ?? ''} size="lg">
      {error && name && (
        <div className="space-y-3">
          <ErrorState compact message="Couldn’t load the how-to." onRetry={retry} />
          <VideoLink href={videoSearchUrl(name)} />
        </div>
      )}
      {!info && !error && <Skeleton className="h-56 sm:h-72 w-full" />}
      {info && !info.found && (
        <div className="space-y-3">
          <p className="text-sm text-content-muted">
            {info.library_available
              ? 'No photo guide for this one yet.'
              : 'The photo library hasn’t been downloaded on the server yet.'}
          </p>
          <VideoLink href={info.video_url} />
        </div>
      )}
      {info?.found && (
        <div className="space-y-4">
          {frames.length > 0 && !imgBroken && (
            <div className="relative h-56 sm:h-72 w-full rounded-lg bg-surface-raised">
              {/* eslint-disable-next-line @next/next/no-img-element -- auth'd proxy image, not optimizable */}
              <img
                src={`/api/proxy${frames[frame]}`}
                alt={altText}
                onError={() => setImgBroken(true)}
                className="h-full w-full object-contain rounded-lg"
              />
              {/* Preload the other frames so the flip doesn't flash. */}
              {frames.map((f, i) =>
                i === frame ? null : (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img key={f} src={`/api/proxy${f}`} alt="" aria-hidden className="hidden" />
                ),
              )}
              {frames.length > 1 && (
                <button
                  type="button"
                  onClick={() => setPlaying((p) => !p)}
                  className={`absolute bottom-2 right-2 flex h-11 w-11 items-center justify-center rounded-full bg-surface-overlay/80 text-content-primary ${FOCUS_RING}`}
                  aria-label={playing ? 'Pause animation' : 'Play animation'}
                >
                  {playing ? <Pause size={18} /> : <Play size={18} />}
                </button>
              )}
            </div>
          )}
          {info.match && info.match !== name && (
            <p className="text-xs text-content-muted">Shown: {info.match}</p>
          )}
          {info.instructions && info.instructions.length > 0 && (
            <ol className="list-decimal pl-5 space-y-1.5 text-sm text-content-primary">
              {info.instructions.map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ol>
          )}
          {info.primary_muscles && info.primary_muscles.length > 0 && (
            <p className="text-xs text-content-muted capitalize">
              Works: {info.primary_muscles.join(', ')}
            </p>
          )}
          <VideoLink href={info.video_url} />
        </div>
      )}
    </Modal>
  );
}
