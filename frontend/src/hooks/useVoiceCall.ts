'use client';

import { useState, useRef, useCallback, useEffect } from 'react';
import { EnergyVad, rmsOf } from '@/lib/vad';
import { pickRecorderMime, transcribe, STTUnavailableError } from '@/lib/stt';

/**
 * Hands-free "call mode": one tap opens the mic for the whole call; the page
 * detects when the user stops talking, transcribes, hands the text to the
 * chat, waits until Jess has finished answering out loud, then listens again.
 *
 * The mic is not analysed while Jess is answering (her own voice would
 * otherwise trigger it) — interrupting is a tap, not barge-in.
 */

export type CallPhase =
  | 'off'
  | 'starting'
  | 'listening' // waiting for the user to start talking
  | 'hearing' // user is talking
  | 'transcribing'
  | 'responding'; // Jess is thinking / speaking

const TICK_MS = 50;
// While nobody talks, restart the recorder periodically so the eventual
// upload doesn't carry minutes of silence.
const IDLE_RESTART_MS = 20_000;
// Let the tail of Jess's audio die down before analysing the mic again.
const RELISTEN_DELAY_MS = 350;
// One undecodable snippet (e.g. a tiny false trigger) shouldn't end the call;
// repeated failures mean something is actually wrong.
const MAX_CONSECUTIVE_STT_FAILURES = 3;

interface WakeLockSentinelLike { release: () => Promise<void> }

export interface UseVoiceCallReturn {
  phase: CallPhase;
  active: boolean;
  /** Why the last call ended on its own (mic/STT failure); null otherwise. */
  error: string | null;
  clearError: () => void;
  /** Must be called synchronously from a tap (creates the AudioContext). */
  start: () => void;
  end: () => void;
}

/**
 * @param onUtterance handles one transcribed utterance; must resolve once
 *   Jess's reply has finished (spoken or stopped) so listening can resume.
 */
export default function useVoiceCall(onUtterance: (text: string) => Promise<unknown>): UseVoiceCallReturn {
  const [phase, setPhase] = useState<CallPhase>('off');
  const [error, setError] = useState<string | null>(null);

  const onUtteranceRef = useRef(onUtterance);
  useEffect(() => { onUtteranceRef.current = onUtterance; }, [onUtterance]);

  // Every async step re-checks this generation; end() bumps it.
  const genRef = useRef(0);
  const ctxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const wakeLockRef = useRef<WakeLockSentinelLike | null>(null);
  const vadRef = useRef<EnergyVad | null>(null);
  const sttFailuresRef = useRef(0);

  const stopTimer = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
  };

  const startRecorder = useCallback(() => {
    const stream = streamRef.current;
    if (!stream) return;
    const { mimeType } = pickRecorderMime();
    const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    chunksRef.current = [];
    rec.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
    rec.start();
    recorderRef.current = rec;
  }, []);

  /** Stop the current recorder; resolves with its audio (or null if discarded). */
  const stopRecorder = useCallback((keep: boolean): Promise<Blob | null> => {
    const rec = recorderRef.current;
    recorderRef.current = null;
    if (!rec || rec.state === 'inactive') return Promise.resolve(null);
    return new Promise((resolve) => {
      rec.onstop = () => resolve(keep ? new Blob(chunksRef.current, { type: rec.mimeType }) : null);
      rec.stop();
    });
  }, []);

  const end = useCallback(() => {
    genRef.current += 1;
    stopTimer();
    abortRef.current?.abort();
    abortRef.current = null;
    void stopRecorder(false);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    analyserRef.current = null;
    ctxRef.current?.close().catch(() => {});
    ctxRef.current = null;
    wakeLockRef.current?.release().catch(() => {});
    wakeLockRef.current = null;
    setPhase('off');
  }, [stopRecorder]);

  const fail = useCallback((message: string) => {
    end();
    setError(message);
  }, [end]);

  const listen = useCallback(() => {
    const gen = genRef.current;
    const analyser = analyserRef.current;
    const vad = vadRef.current;
    if (!analyser || !vad) return;
    setPhase('listening');
    vad.reset();
    try {
      startRecorder();
    } catch {
      fail('Couldn’t start recording on this browser.');
      return;
    }
    const buf = new Float32Array(analyser.fftSize);
    let idleSince = performance.now();

    const finishUtterance = async () => {
      const blob = await stopRecorder(true);
      if (gen !== genRef.current) return;
      if (!blob || blob.size === 0) { listen(); return; }
      setPhase('transcribing');
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      let text: string;
      try {
        text = await transcribe(blob, ctrl.signal);
      } catch (err) {
        if (gen !== genRef.current) return;
        if (err instanceof STTUnavailableError) {
          fail('Jess can’t hear right now — Helios (speech recognition) is asleep.');
        } else if (++sttFailuresRef.current >= MAX_CONSECUTIVE_STT_FAILURES) {
          fail('Speech recognition keeps failing, so the call ended.');
        } else {
          listen(); // skip this snippet, keep the call going
        }
        return;
      }
      if (gen !== genRef.current) return;
      sttFailuresRef.current = 0;
      if (!text) { listen(); return; } // cough / noise: nothing heard
      setPhase('responding');
      try {
        await onUtteranceRef.current(text);
      } catch { /* the chat page shows its own error */ }
      if (gen !== genRef.current) return;
      setPhase('listening'); // label now; the mic is analysed after the delay
      setTimeout(() => { if (gen === genRef.current) listen(); }, RELISTEN_DELAY_MS);
    };

    timerRef.current = setInterval(() => {
      if (gen !== genRef.current) { stopTimer(); return; }
      analyser.getFloatTimeDomainData(buf);
      const now = performance.now();
      const ev = vad.feed(rmsOf(buf), now);
      if (ev === 'speech-start') {
        setPhase('hearing');
      } else if (ev === 'speech-end') {
        stopTimer();
        void finishUtterance();
      } else if (!vad.isSpeaking && now - idleSince > IDLE_RESTART_MS) {
        idleSince = now;
        void stopRecorder(false).then(() => { if (gen === genRef.current) startRecorder(); });
      }
    }, TICK_MS);
  }, [fail, startRecorder, stopRecorder]);

  const start = useCallback(() => {
    if (ctxRef.current) return;
    setError(null);
    const gen = ++genRef.current;
    // Created + resumed inside the tap: iOS keeps a context suspended otherwise.
    const Ctx = window.AudioContext
      ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setError('Hands-free mode isn’t supported in this browser.');
      return;
    }
    const ctx = new Ctx();
    ctx.resume().catch(() => {});
    ctxRef.current = ctx;
    vadRef.current = new EnergyVad();
    sttFailuresRef.current = 0;
    setPhase('starting');

    void (async () => {
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch {
        if (gen === genRef.current) fail('Microphone access was denied.');
        return;
      }
      if (gen !== genRef.current) { stream.getTracks().forEach((t) => t.stop()); return; }
      streamRef.current = stream;
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      ctx.createMediaStreamSource(stream).connect(analyser);
      analyserRef.current = analyser;
      try {
        const wl = (navigator as unknown as { wakeLock?: { request: (t: 'screen') => Promise<WakeLockSentinelLike> } }).wakeLock;
        if (wl) wakeLockRef.current = await wl.request('screen');
      } catch { /* screen may lock; the call ends when the page is hidden */ }
      if (gen === genRef.current) listen();
    })();
  }, [fail, listen]);

  // iOS suspends a hidden page (and its mic) — end cleanly instead of hanging.
  useEffect(() => {
    const onHide = () => { if (document.hidden && ctxRef.current) end(); };
    document.addEventListener('visibilitychange', onHide);
    return () => document.removeEventListener('visibilitychange', onHide);
  }, [end]);

  useEffect(() => () => { if (ctxRef.current) end(); }, [end]);

  return {
    phase,
    active: phase !== 'off',
    error,
    clearError: useCallback(() => setError(null), []),
    start,
    end,
  };
}
