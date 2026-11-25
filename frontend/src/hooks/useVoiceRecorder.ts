'use client';

import { useState, useRef, useCallback, useEffect } from 'react';
import { MIC_CONSTRAINTS, pickRecorderMime, transcribe, STTUnavailableError } from '@/lib/stt';
import { setAudioSessionType } from '@/lib/audio-session';

/**
 * Push-to-talk mic button: tap to record, tap again to transcribe.
 *
 * Opens the mic the way hands-free call mode does. Call mode kept working on
 * an iPhone when this button started recording near-silence (Nov 2026: one
 * recording transcribed to nothing, another to a single word): the audio
 * session goes to 'play-and-record' BEFORE getUserMedia, with voice
 * processing on (MIC_CONSTRAINTS). Releasing the mic hands the session back
 * to 'auto', so the spoken reply plays through the normal media path.
 *
 * Never gives up silently: every way a recording can fail shows its own
 * message. Each stop writes one console line with the duration, size, MIME
 * type and the mic track's state (and the transcript's length, never its
 * text) for Safari's Web Inspector: "recorded near-silence" vs "cut off" vs
 * "the phone muted the mic" can be told apart from it.
 *
 * Privacy: the mic is open only between the two taps. A recording the
 * browser ends on its own, one left running when the page is hidden (or the
 * mic finished opening on a hidden page), and one past MAX_RECORDING_MS are
 * dropped, never uploaded (half a sentence, or a room the user forgot about,
 * must not go to Jess). A recording the browser had already finished when the
 * user tapped stop is still transcribed. While recording, a screen wake lock
 * (as in call mode) keeps auto-lock from hiding the page mid-sentence.
 */

export const NOTHING_HEARD = 'Didn’t catch anything — try again.';
export const NOTHING_RECORDED = 'Nothing was recorded — try again.';
export const MIC_MUTED = 'Your phone muted the microphone while recording — try again.';
export const STOPPED_EARLY = 'The recording stopped on its own — tap the mic to try again.';
export const LEFT_PAGE = 'Recording stopped when you left the page — tap the mic to try again.';
export const TOO_LONG = 'Recording stopped after 3 minutes — tap the mic to try again.';
export const NOT_FINISHED = 'The recording didn’t finish — try again.';
// MediaRecorder's stop event normally follows stop() within milliseconds; a
// browser that never fires it must not leave the button stuck on "Listening".
export const STOP_TIMEOUT_MS = 3_000;
// A mic left open by mistake stops itself (the server caps uploads anyway).
export const MAX_RECORDING_MS = 3 * 60_000;
// WebKit has hung in getUserMedia; without a limit every later tap would be
// ignored until reload. Long enough to answer the permission prompt.
export const MIC_OPEN_TIMEOUT_MS = 20_000;
export const MIC_OPEN_FAILED = 'The microphone didn’t open — try again.';

interface Recording {
  recorder: MediaRecorder;
  stream: MediaStream;
  chunks: Blob[];
  startedAt: number;
  /** True once the recording is over for us (stop tap, drop, unmount). */
  stopping: boolean;
  /** Resolves on the recorder's stop event. */
  stopped: Promise<void>;
  /** How often the browser muted the mic track while recording. */
  mutes: number;
  limitTimer?: ReturnType<typeof setTimeout>;
  wakeLock?: WakeLockSentinelLike;
}

interface WakeLockSentinelLike { release: () => Promise<void> }

interface UseVoiceRecorderReturn {
  /** The mic is being opened (permission, audio session): other audio buttons wait. */
  isStarting: boolean;
  isRecording: boolean;
  isTranscribing: boolean;
  startRecording: () => Promise<void>;
  /**
   * Stops the recorder synchronously (before its first await), so a caller
   * can do gesture-bound work right after calling it, inside the same tap.
   * Resolves with the trimmed transcript, or null (the error says why).
   */
  stopRecording: () => Promise<string | null>;
  error: string | null;
  clearError: () => void;
}

const sinceStart = (rec: Recording) => Math.round(performance.now() - rec.startedAt);

/** Numbers and flags only: what the phone actually gave us (it may ignore constraints). */
function describeMic(track: MediaStreamTrack | undefined): string {
  const st = (track?.getSettings?.() ?? {}) as MediaTrackSettings;
  const session = (navigator as unknown as { audioSession?: { type?: string; state?: string } }).audioSession;
  const flag = (v: boolean | undefined) => (v === undefined ? '?' : v ? 'on' : 'off');
  return `session ${session?.type ?? 'n/a'}/${session?.state ?? 'n/a'}, `
    + `echo ${flag(st.echoCancellation)}, noise ${flag(st.noiseSuppression)}, gain ${flag(st.autoGainControl)}, `
    + `${st.sampleRate ?? '?'} Hz, ${st.channelCount ?? '?'} ch`;
}

/** getUserMedia with a time limit; a stream that arrives too late is closed. */
async function openMic(): Promise<MediaStream> {
  const opening = navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      opening,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          opening.then((late) => late.getTracks().forEach((t) => t.stop()), () => {});
          reject(Object.assign(new Error('the microphone did not open in time'), { name: 'MicOpenTimeout' }));
        }, MIC_OPEN_TIMEOUT_MS);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

/** Ask for a screen wake lock (Safari 16.4+); null where unsupported or refused. */
function requestWakeLock(): Promise<WakeLockSentinelLike | null> {
  const wl = (navigator as unknown as { wakeLock?: { request: (t: 'screen') => Promise<WakeLockSentinelLike> } }).wakeLock;
  if (!wl) return Promise.resolve(null);
  try {
    return wl.request('screen').catch(() => null); // the screen may lock: the page-hidden drop covers it
  } catch {
    return Promise.resolve(null);
  }
}

const releaseWakeLock = (lock: WakeLockSentinelLike | null | undefined) => { lock?.release().catch(() => {}); };

function micTrack(rec: Recording): MediaStreamTrack | undefined {
  return rec.stream.getAudioTracks?.()[0];
}

/** Stop the recorder (if it still runs) and close the mic. Idempotent. */
function closeRecording(rec: Recording): void {
  rec.stopping = true;
  clearTimeout(rec.limitTimer);
  try {
    if (rec.recorder.state !== 'inactive') rec.recorder.stop();
  } catch { /* the browser stopped it a moment ago */ }
  const track = micTrack(rec);
  if (track) {
    track.onmute = null;
    track.onunmute = null;
  }
  rec.stream.getTracks().forEach((t) => t.stop());
  setAudioSessionType('auto');
  releaseWakeLock(rec.wakeLock);
  rec.wakeLock = undefined;
}

export default function useVoiceRecorder(): UseVoiceRecorderReturn {
  const [isStarting, setIsStarting] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recRef = useRef<Recording | null>(null);
  const startingRef = useRef(false);
  const mountedRef = useRef(true);

  /** End a recording without uploading it, and say why. */
  const drop = useCallback((rec: Recording, message: string, why: string) => {
    if (recRef.current !== rec) return;
    recRef.current = null;
    closeRecording(rec);
    console.warn(`[mic] recording dropped (${why}) after ${sinceStart(rec)} ms`);
    if (!mountedRef.current) return;
    setIsRecording(false);
    setError(message);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const onVisibility = () => {
      const rec = recRef.current;
      if (rec && document.visibilityState === 'hidden') drop(rec, LEFT_PAGE, 'page hidden');
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      // Leaving the chat mid-recording must not leave the mic open.
      mountedRef.current = false;
      document.removeEventListener('visibilitychange', onVisibility);
      const rec = recRef.current;
      recRef.current = null;
      if (rec) closeRecording(rec);
    };
  }, [drop]);

  const startRecording = useCallback(async () => {
    if (startingRef.current || recRef.current) return; // a second tap while the mic opens
    startingRef.current = true;
    setIsStarting(true);
    setError(null);
    let stream: MediaStream | null = null;
    let recording = false;
    const tappedAt = performance.now();
    // Asked for inside the tap (before any await): WebKit may want the gesture.
    const wakeLock = requestWakeLock();
    try {
      // The order call mode uses: session type first, then the mic. 'playback'
      // (left by a call) or 'auto' while a clip plays can make iOS capture silence.
      setAudioSessionType('play-and-record');
      stream = await openMic();
      if (!mountedRef.current || document.visibilityState === 'hidden') {
        // Left /chat (not for a call: ChatInput keeps the call button disabled
        // while isStarting, so 'auto' can't undo its session) or the page went
        // hidden while the mic opened: no hidden-page event will come now.
        stream.getTracks().forEach((t) => t.stop());
        setAudioSessionType('auto');
        if (mountedRef.current) {
          console.warn('[mic] page hidden while the mic opened');
          setError(LEFT_PAGE);
        }
        return;
      }
      const { mimeType } = pickRecorderMime();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      let markStopped: () => void = () => {};
      const rec: Recording = {
        recorder,
        stream,
        chunks: [],
        startedAt: performance.now(),
        stopping: false,
        stopped: new Promise<void>((resolve) => { markStopped = resolve; }),
        mutes: 0,
      };
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) rec.chunks.push(e.data);
      };
      recorder.onerror = (e) => {
        const name = (e as unknown as { error?: { name?: string } }).error?.name;
        console.warn('[mic] recorder error:', name || 'unknown');
      };
      recorder.onstop = () => {
        markStopped();
        // The browser ended it (an audio-session change, a phone call or Siri,
        // another app taking the mic): whatever was said after that is lost.
        if (!rec.stopping) drop(rec, STOPPED_EARLY, 'stopped by the browser');
      };
      const track = micTrack(rec);
      if (track) {
        // iOS mutes rather than ends the track on most interruptions; the
        // recording then holds silence. Logged, and named if nothing is heard.
        track.onmute = () => {
          // After the stop tap (the reply-audio unlock plays then) it says
          // nothing about the recording: logged, not counted.
          if (!rec.stopping) rec.mutes += 1;
          console.warn(`[mic] microphone muted by the browser at ${sinceStart(rec)} ms${rec.stopping ? ', after stop' : ''}`);
        };
        track.onunmute = () => console.warn(`[mic] microphone unmuted at ${sinceStart(rec)} ms`);
      }
      recorder.start();
      // Words said before this point are lost: the placeholder says "Opening the mic…" until then.
      console.warn(`[mic] recording ${Math.round(performance.now() - tappedAt)} ms after the tap; ${describeMic(track)}`);
      recRef.current = rec;
      recording = true;
      rec.limitTimer = setTimeout(() => drop(rec, TOO_LONG, 'time limit'), MAX_RECORDING_MS);
      void wakeLock.then((lock) => {
        if (rec.stopping) releaseWakeLock(lock);
        else if (lock) rec.wakeLock = lock;
      });
      setIsRecording(true);
    } catch (err) {
      // Release the mic if it was acquired before the error (e.g. the MediaRecorder constructor threw).
      stream?.getTracks().forEach((t) => t.stop());
      setAudioSessionType('auto');
      // getUserMedia rejects with a DOMException, which isn't an Error everywhere.
      const raw = (err as { name?: unknown } | null)?.name;
      const name = typeof raw === 'string' ? raw : '';
      console.warn('[mic] start failed:', name || 'unknown');
      setError(
        !navigator.mediaDevices?.getUserMedia ? 'The microphone needs the secure (https://) dashboard address.'
          : name === 'NotAllowedError' ? 'Microphone access is blocked — allow it for this site in your browser settings, then reload.'
            : name === 'NotFoundError' ? 'No microphone found.'
              : name === 'NotReadableError' ? 'The microphone is busy in another app.'
                : name === 'NotSupportedError' ? 'Recording isn’t supported in this browser.'
                  : name === 'MicOpenTimeout' ? MIC_OPEN_FAILED
                    : 'Couldn’t start the microphone — try reloading the page.',
      );
    } finally {
      if (!recording) void wakeLock.then(releaseWakeLock);
      startingRef.current = false;
      if (mountedRef.current) setIsStarting(false);
    }
  }, [drop]);

  const stopRecording = useCallback(async (): Promise<string | null> => {
    const rec = recRef.current;
    if (!rec) return null;
    recRef.current = null;
    rec.stopping = true;
    clearTimeout(rec.limitTimer);
    // Synchronous, before any await: see the interface comment.
    if (rec.recorder.state !== 'inactive') {
      try {
        rec.recorder.stop();
      } catch { /* the browser stopped it a moment ago; its stop event still comes */ }
    }

    let timer: ReturnType<typeof setTimeout> | undefined;
    const timedOut = await Promise.race([
      rec.stopped.then(() => false),
      new Promise<boolean>((resolve) => { timer = setTimeout(() => resolve(true), STOP_TIMEOUT_MS); }),
    ]);
    clearTimeout(timer);
    const track = micTrack(rec);
    const trackState = track ? `${track.readyState}${track.muted ? ' (muted now)' : ''}` : 'none';
    closeRecording(rec);
    if (!mountedRef.current) return null; // left /chat: nothing is sent
    setIsRecording(false);

    const blob = new Blob(rec.chunks, { type: rec.recorder.mimeType });
    console.warn(
      `[mic] recorded ${sinceStart(rec)} ms, ${blob.size} bytes, ${blob.type || 'no type'}; `
      + `track ${trackState}, mutes ${rec.mutes}${timedOut ? `; no stop event in ${STOP_TIMEOUT_MS} ms` : ''}`,
    );
    if (blob.size === 0) {
      setError(timedOut ? NOT_FINISHED : NOTHING_RECORDED);
      return null;
    }

    setIsTranscribing(true);
    try {
      const text = await transcribe(blob);
      if (!mountedRef.current) return null;
      if (!text) {
        console.warn('[mic] nothing heard');
        setError(rec.mutes ? MIC_MUTED : NOTHING_HEARD);
        return null;
      }
      console.warn(`[mic] heard ${text.length} characters`);
      return text;
    } catch (err) {
      // "STT <status>" or the browser's network error: no content.
      console.warn('[mic] transcribe failed:', err instanceof Error ? err.message : 'unknown');
      setError(err instanceof STTUnavailableError
        ? 'Jess can’t hear right now — Helios (speech recognition) is asleep.'
        : 'Couldn’t transcribe that — please try again.');
      return null;
    } finally {
      if (mountedRef.current) setIsTranscribing(false);
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);

  return { isStarting, isRecording, isTranscribing, startRecording, stopRecording, error, clearError };
}
