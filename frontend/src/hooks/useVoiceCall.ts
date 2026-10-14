'use client';

import { useState, useRef, useCallback, useEffect } from 'react';
import { EnergyVad, rmsOf } from '@/lib/vad';
import { pickRecorderMime, transcribe, STTUnavailableError } from '@/lib/stt';
import { setAudioSessionType } from '@/lib/audio-session';

/**
 * Hands-free "call mode": one tap opens the mic for the whole call; the page
 * detects when the user stops talking, transcribes, hands the text to the
 * chat, waits until Jess has finished answering out loud, then listens again.
 *
 * The mic is not analysed while Jess is answering (her own voice would
 * otherwise trigger it) — interrupting is a tap, not barge-in.
 *
 * Privacy/safety rules (security review): only the utterance plus at most
 * ~IDLE_RESTART_MS of lead-in is ever uploaded; audio the VAD classifies as
 * steady noise ('speech-timeout') is discarded, never transcribed; an open
 * call with nobody talking ends itself after IDLE_CALL_END_MS.
 */

export type CallPhase =
  | 'off'
  | 'starting'
  | 'listening' // waiting for the user to start talking
  | 'hearing' // user is talking
  | 'transcribing'
  | 'responding'; // Jess is thinking / speaking

const TICK_MS = 50;
// While nobody talks the recorder is restarted this often and the old audio
// dropped — bounds both upload size and how much room audio from BEFORE the
// user spoke can ride along with an utterance.
const IDLE_RESTART_MS = 3_000;
// Let the tail of Jess's audio die down before analysing the mic again.
const RELISTEN_DELAY_MS = 350;
// One undecodable snippet (e.g. a tiny false trigger) shouldn't end the call;
// repeated failures mean something is actually wrong.
const MAX_CONSECUTIVE_STT_FAILURES = 3;
// Noise that keeps transcribing to nothing backs off: 1 s, 2 s, 4 s, 5 s…
const EMPTY_BACKOFF_MAX_MS = 5_000;
// End an open call when nobody has said anything for this long.
const IDLE_CALL_END_MS = 5 * 60_000;
// How long iOS gets to resume an interrupted AudioContext (call/Siri/alarm).
const INTERRUPT_GRACE_MS = 1_500;
// While the mic is open iOS runs the whole audio session in "phone call"
// mode (voice processing): Jess's reply sounds like a walkie-talkie, with
// static/pops. So the mic is released as soon as an utterance is recorded and
// re-acquired after her reply — she then plays through the normal media path.
const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
};

interface WakeLockSentinelLike { release: () => Promise<void> }

interface Recording {
  rec: MediaRecorder;
  chunks: Blob[];
}

export interface UseVoiceCallReturn {
  phase: CallPhase;
  active: boolean;
  /** Why the last call ended on its own (mic/STT failure, idle); null otherwise. */
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
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const recordingRef = useRef<Recording | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const wakeLockRef = useRef<WakeLockSentinelLike | null>(null);
  const vadRef = useRef<EnergyVad | null>(null);
  const sttFailuresRef = useRef(0);
  const emptyRunRef = useRef(0);
  const lastHeardRef = useRef(0); // performance.now() of the last real utterance (or call start)

  const stopTimer = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
  };

  const newRecording = useCallback((): Recording | null => {
    const stream = streamRef.current;
    if (!stream) return null;
    const { mimeType } = pickRecorderMime();
    const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    const r: Recording = { rec, chunks: [] };
    rec.ondataavailable = (e) => { if (e.data.size > 0) r.chunks.push(e.data); };
    rec.start();
    return r;
  }, []);

  /** Stop a recording; resolves with its audio when `keep`, else null (dropped in the browser). */
  const finishRecording = useCallback((r: Recording | null, keep: boolean): Promise<Blob | null> => {
    if (!r || r.rec.state === 'inactive') return Promise.resolve(null);
    return new Promise((resolve) => {
      r.rec.onstop = () => {
        const blob = keep ? new Blob(r.chunks, { type: r.rec.mimeType }) : null;
        r.chunks.length = 0;
        resolve(blob);
      };
      r.rec.stop();
    });
  }, []);

  /** Close the mic (iOS leaves "phone call" audio mode once nothing captures). */
  const releaseMic = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => { t.onended = null; t.stop(); });
    streamRef.current = null;
    sourceRef.current?.disconnect();
    sourceRef.current = null;
    setAudioSessionType('playback');
  }, []);

  const end = useCallback(() => {
    genRef.current += 1;
    stopTimer();
    abortRef.current?.abort();
    abortRef.current = null;
    void finishRecording(recordingRef.current, false);
    recordingRef.current = null;
    releaseMic();
    analyserRef.current = null;
    const ctx = ctxRef.current;
    ctxRef.current = null;
    if (ctx) {
      ctx.onstatechange = null;
      ctx.close().catch(() => {});
    }
    wakeLockRef.current?.release().catch(() => {});
    wakeLockRef.current = null;
    // releaseMic() left the session on 'playback', which blocks capture on
    // iOS — hand it back or the plain mic button can't record afterwards.
    setAudioSessionType('auto');
    setPhase('off');
  }, [finishRecording, releaseMic]);

  const fail = useCallback((message: string) => {
    end();
    setError(message);
  }, [end]);

  /**
   * Make sure the AudioContext is running before analysing. WebKit can leave
   * resume() pending for as long as an iOS interruption lasts (call, Siri,
   * alarm), so the wait is bounded; if it is still down the call ends with a
   * message instead of sitting on "Listening" reading silence.
   */
  const ensureRunning = useCallback(async (gen: number): Promise<boolean> => {
    const ctx = ctxRef.current;
    if (!ctx) return false;
    if (ctx.state !== 'running') {
      await Promise.race([
        ctx.resume().catch(() => {}),
        new Promise((r) => setTimeout(r, INTERRUPT_GRACE_MS)),
      ]);
    }
    if (gen !== genRef.current) return false;
    if (ctx.state !== 'running') {
      fail('The call was paused by your phone — tap the call button to start again.');
      return false;
    }
    return true;
  }, [fail]);

  /** Open the mic and feed the analyser. False (and the call ended) on failure. */
  const acquireMic = useCallback(async (gen: number, failMessage: string): Promise<boolean> => {
    const ctx = ctxRef.current;
    const analyser = analyserRef.current;
    if (!ctx || !analyser) return false;
    let stream: MediaStream;
    setAudioSessionType('play-and-record');
    try {
      stream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
    } catch (err) {
      // NotAllowedError (permission), NotReadableError (another app has the
      // mic), NotFoundError… — keep the name for on-device debugging.
      console.warn('[call] getUserMedia failed:', err instanceof Error ? err.name : err);
      if (gen === genRef.current) fail(failMessage);
      return false;
    }
    if (gen !== genRef.current) { stream.getTracks().forEach((t) => t.stop()); return false; }
    let source: MediaStreamAudioSourceNode;
    try {
      source = ctx.createMediaStreamSource(stream);
      source.connect(analyser);
    } catch (err) {
      console.warn('[call] mic source setup failed:', err instanceof Error ? err.name : err);
      stream.getTracks().forEach((t) => t.stop());
      fail(failMessage);
      return false;
    }
    streamRef.current = stream;
    sourceRef.current = source;
    stream.getAudioTracks().forEach((t) => {
      t.onended = () => { if (gen === genRef.current) fail('The microphone was disconnected, so the call ended.'); };
    });
    return true;
  }, [fail]);

  const listen = useCallback(() => {
    const gen = genRef.current;
    const analyser = analyserRef.current;
    const vad = vadRef.current;
    if (!analyser || !vad) return;
    setPhase('listening');
    vad.reset();
    try {
      recordingRef.current = newRecording();
    } catch {
      fail('Couldn’t start recording on this browser.');
      return;
    }
    const buf = new Float32Array(analyser.fftSize);
    let idleSince = performance.now();

    /** Re-open the mic if it was released, then listen again after `delayMs`. */
    const relisten = (delayMs: number) => {
      setPhase('listening'); // label now; the mic is analysed after the delay
      setTimeout(async () => {
        if (gen !== genRef.current) return;
        // onstatechange is ignored while the mic is released, so an iOS
        // interruption during the reply is caught here — before the mic is
        // re-opened, so it is never held while waiting on a stuck context.
        if (!(await ensureRunning(gen))) return;
        if (!streamRef.current) {
          const ok = await acquireMic(gen, 'Couldn’t get the microphone back — tap the call button to start again.');
          if (!ok || gen !== genRef.current) return;
        }
        listen();
      }, delayMs);
    };

    const finishUtterance = async () => {
      const blob = await finishRecording(recordingRef.current, true);
      recordingRef.current = null;
      if (gen !== genRef.current) return;
      // Recording done: let go of the mic before Jess speaks (see MIC_CONSTRAINTS).
      releaseMic();
      if (!blob || blob.size === 0) { relisten(0); return; }
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
          relisten(0); // skip this snippet, keep the call going
        }
        return;
      }
      if (gen !== genRef.current) return;
      sttFailuresRef.current = 0;
      if (!text) {
        // Cough / noise: nothing heard. Back off so a noisy room doesn't
        // hammer the STT server.
        emptyRunRef.current += 1;
        relisten(Math.min(1000 * 2 ** (emptyRunRef.current - 1), EMPTY_BACKOFF_MAX_MS));
        return;
      }
      emptyRunRef.current = 0;
      lastHeardRef.current = performance.now();
      setPhase('responding');
      try {
        await onUtteranceRef.current(text);
      } catch { /* the chat page shows its own error */ }
      if (gen !== genRef.current) return;
      lastHeardRef.current = performance.now();
      relisten(RELISTEN_DELAY_MS);
    };

    timerRef.current = setInterval(() => {
      if (gen !== genRef.current) { stopTimer(); return; }
      const now = performance.now();
      if (now - lastHeardRef.current > IDLE_CALL_END_MS) {
        fail('Call ended — nobody has said anything for 5 minutes.');
        return;
      }
      analyser.getFloatTimeDomainData(buf);
      const ev = vad.feed(rmsOf(buf), now);
      if (ev === 'speech-start') {
        setPhase('hearing');
      } else if (ev === 'speech-end') {
        stopTimer();
        void finishUtterance();
      } else if (ev === 'speech-timeout') {
        // Steady noise (TV/radio) — drop it in the browser, never transcribe.
        stopTimer();
        void finishRecording(recordingRef.current, false).then(() => {
          recordingRef.current = null;
          if (gen === genRef.current) listen();
        });
      } else if (!vad.isSpeaking && !vad.isRising && now - idleSince > IDLE_RESTART_MS) {
        idleSince = now;
        // Start the new recording before dropping the old one: no gap.
        const old = recordingRef.current;
        try {
          recordingRef.current = newRecording();
        } catch {
          fail('Couldn’t start recording on this browser.');
          return;
        }
        void finishRecording(old, false);
      }
    }, TICK_MS);
  }, [acquireMic, ensureRunning, fail, finishRecording, newRecording, releaseMic]);

  const start = useCallback(() => {
    if (ctxRef.current) return;
    setError(null);
    // Created + resumed inside the tap: iOS keeps a context suspended otherwise.
    const Ctx = window.AudioContext
      ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setError('Hands-free mode isn’t supported in this browser.');
      return;
    }
    const gen = ++genRef.current;
    const ctx = new Ctx();
    ctx.resume().catch(() => {});
    ctxRef.current = ctx;
    vadRef.current = new EnergyVad();
    sttFailuresRef.current = 0;
    emptyRunRef.current = 0;
    lastHeardRef.current = performance.now();
    setPhase('starting');

    // iOS moves the context to 'interrupted' for a phone call, Siri or an
    // alarm — the analyser then reads silence and the call would sit on
    // "Listening" forever. Try to resume; if it stays down, end visibly.
    ctx.onstatechange = () => {
      if (gen !== genRef.current || ctx.state === 'running' || ctx.state === 'closed') return;
      // Mic released while Jess answers: the session switch may bounce the
      // context; relisten() resumes it before analysing again.
      if (!streamRef.current) return;
      ctx.resume().catch(() => {});
      setTimeout(() => {
        if (gen === genRef.current && ctx.state !== 'running') {
          fail('The call was paused by your phone — tap the call button to start again.');
        }
      }, INTERRUPT_GRACE_MS);
    };

    // The analyser outlives each mic stream (re-acquired every turn).
    // WebKit may not pull nodes with no path to the destination; a muted
    // gain keeps the analyser fed without playing the mic back.
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    const mute = ctx.createGain();
    mute.gain.value = 0;
    analyser.connect(mute);
    mute.connect(ctx.destination);
    analyserRef.current = analyser;

    void (async () => {
      if (!(await acquireMic(gen, 'Microphone access was denied.'))) return;
      // An interruption during the permission prompt is ignored by
      // onstatechange (no stream yet) — check before the first listen.
      if (!(await ensureRunning(gen))) return;
      try {
        const wl = (navigator as unknown as { wakeLock?: { request: (t: 'screen') => Promise<WakeLockSentinelLike> } }).wakeLock;
        if (wl) {
          const sentinel = await wl.request('screen');
          if (gen !== genRef.current) { sentinel.release().catch(() => {}); return; }
          wakeLockRef.current = sentinel;
        }
      } catch { /* screen may lock; the call ends when the page is hidden */ }
      if (gen === genRef.current) listen();
    })();
  }, [acquireMic, ensureRunning, fail, listen]);

  // iOS suspends a hidden page (and its mic) — end cleanly instead of hanging.
  useEffect(() => {
    const onHide = () => { if (document.hidden && ctxRef.current) end(); };
    document.addEventListener('visibilitychange', onHide);
    return () => document.removeEventListener('visibilitychange', onHide);
  }, [end]);

  useEffect(() => () => { if (ctxRef.current) end(); }, [end]);

  const clearError = useCallback(() => setError(null), []);

  return { phase, active: phase !== 'off', error, clearError, start, end };
}
