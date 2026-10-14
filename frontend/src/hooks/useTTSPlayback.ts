'use client';

import { useState, useRef, useCallback, useEffect } from 'react';
import {
  SpeechQueue, TTSUnavailableError, type SpeechPlayer, type SpeechState, type SpeechStatus,
} from '@/lib/speech-queue';
import { SpeechSegmenter, segmentForSpeech } from '@/lib/speech-text';

const PROXY = '/api/proxy';
const LS_KEY = 'tts_enabled';
// Per-sentence synth normally takes 0.5–3 s; a hung TTS (e.g. Helios mid-boot)
// must not leave the UI on "loading" forever.
const SYNTH_TIMEOUT_MS = 30_000;

/**
 * 0.05 s of 8 kHz mono silence as a WAV data URI. Playing it inside a tap is
 * what "unlocks" the shared <audio> element on iOS Safari: WebKit only lets
 * an element start audio without a gesture once that same element has been
 * played from a gesture, and our replies arrive seconds after the tap.
 */
const SILENT_WAV = (() => {
  const samples = 400;
  const buf = new ArrayBuffer(44 + samples * 2);
  const v = new DataView(buf);
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + samples * 2, true); str(8, 'WAVE');
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 8000, true); v.setUint32(28, 16000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, 'data'); v.setUint32(40, samples * 2, true);
  let bin = '';
  new Uint8Array(buf).forEach((b) => { bin += String.fromCharCode(b); });
  return `data:audio/wav;base64,${typeof btoa === 'function' ? btoa(bin) : ''}`;
})();

async function synthesize(text: string, signal: AbortSignal): Promise<Blob> {
  // Manual timeout + chaining (AbortSignal.any needs Safari 17.4+).
  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort();
  signal.addEventListener('abort', onAbort, { once: true });
  const timer = setTimeout(onAbort, SYNTH_TIMEOUT_MS);
  try {
    const res = await fetch(`${PROXY}/api/tts/synthesize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
      signal: ctrl.signal,
    });
    if (res.status === 503) throw new TTSUnavailableError();
    if (!res.ok) throw new Error(`TTS ${res.status}`);
    const ct = res.headers.get('content-type') || '';
    if (!ct.startsWith('audio/')) throw new Error(`TTS unexpected ${ct}`);
    return await res.blob();
  } finally {
    clearTimeout(timer);
    signal.removeEventListener('abort', onAbort);
  }
}

/** One long-lived <audio> element, reused for every clip (see SILENT_WAV). */
class ElementPlayer implements SpeechPlayer {
  private el: HTMLAudioElement | null = null;
  private url: string | null = null;
  private rejectPending: ((e: Error) => void) | null = null;

  private ensure(): HTMLAudioElement {
    if (!this.el) {
      this.el = new Audio();
      this.el.preload = 'auto';
      this.el.setAttribute('playsinline', '');
    }
    return this.el;
  }

  /** Must run synchronously inside a user-gesture handler. */
  unlock(): void {
    const el = this.ensure();
    if (!el.paused) return; // already playing → already unlocked
    el.onended = null;
    el.onerror = null;
    el.onpause = null;
    el.src = SILENT_WAV;
    el.play().catch(() => { /* superseded by a real clip, or not a gesture */ });
  }

  play(audio: Blob): Promise<void> {
    const el = this.ensure();
    this.releaseUrl();
    this.url = URL.createObjectURL(audio);
    return new Promise<void>((resolve, reject) => {
      this.rejectPending = reject;
      el.onended = () => { this.rejectPending = null; resolve(); };
      el.onerror = () => { this.rejectPending = null; reject(new Error('audio decode/playback error')); };
      // iOS pauses the element itself on screen lock / call / Siri / Control
      // Center. Without this the clip promise would never settle. A natural
      // end also fires 'pause' (just before 'ended'), hence the ended guard.
      el.onpause = () => {
        if (el.ended) return;
        this.rejectPending = null;
        reject(Object.assign(new Error('playback paused by the system'), { name: 'MediaInterrupted' }));
      };
      el.src = this.url!;
      el.play().catch((err: unknown) => {
        this.rejectPending = null;
        reject(err instanceof Error ? err : new Error(String(err)));
      });
    });
  }

  stop(): void {
    if (this.el) {
      this.el.onended = null;
      this.el.onerror = null;
      this.el.onpause = null;
      this.el.pause();
    }
    const reject = this.rejectPending;
    this.rejectPending = null;
    reject?.(Object.assign(new Error('stopped'), { name: 'AbortError' }));
    this.releaseUrl();
  }

  private releaseUrl(): void {
    if (this.url) {
      URL.revokeObjectURL(this.url);
      this.url = null;
    }
  }
}

export interface UseTTSPlaybackReturn {
  ttsEnabled: boolean;
  setTtsEnabled: (enabled: boolean) => void;
  /** Current playback status and the message key it belongs to. */
  speech: SpeechState;
  /** True while a reply is being synthesized or played. */
  isSpeaking: boolean;
  /** Call synchronously from every tap/keypress that may lead to speech. */
  unlock: () => void;
  /**
   * Streaming reply: begin → pushText(chunk)* → endReply. The promise
   * resolves when the reply has finished speaking, failed, or was stopped.
   */
  beginReply: (key: string) => Promise<SpeechStatus>;
  pushText: (chunk: string) => void;
  endReply: () => void;
  /** Speak a finished message (replay / tap to play). Call from a tap. */
  speakText: (key: string, text: string) => void;
  stop: () => void;
}

export default function useTTSPlayback(): UseTTSPlaybackReturn {
  const [ttsEnabled, setTtsEnabledState] = useState(false);
  const [speech, setSpeech] = useState<SpeechState>({ status: 'idle', key: null });
  const playerRef = useRef<ElementPlayer | null>(null);
  const queueRef = useRef<SpeechQueue | null>(null);
  const segmenterRef = useRef<SpeechSegmenter | null>(null);

  const getQueue = useCallback((): SpeechQueue => {
    if (!playerRef.current) playerRef.current = new ElementPlayer();
    if (!queueRef.current) {
      queueRef.current = new SpeechQueue(synthesize, playerRef.current, setSpeech);
    }
    return queueRef.current;
  }, []);

  // Hydrate from localStorage after mount
  useEffect(() => {
    try {
      setTtsEnabledState(localStorage.getItem(LS_KEY) === 'true');
    } catch { /* storage blocked — default off */ }
  }, []);

  const setTtsEnabled = useCallback((enabled: boolean) => {
    setTtsEnabledState(enabled);
    try {
      localStorage.setItem(LS_KEY, String(enabled));
    } catch { /* preference simply won't persist */ }
  }, []);

  const unlock = useCallback(() => {
    getQueue();
    playerRef.current!.unlock();
  }, [getQueue]);

  const stop = useCallback(() => {
    segmenterRef.current = null;
    queueRef.current?.stop();
  }, []);

  const beginReply = useCallback((key: string) => {
    segmenterRef.current = new SpeechSegmenter();
    return getQueue().begin(key);
  }, [getQueue]);

  const pushText = useCallback((chunk: string) => {
    const seg = segmenterRef.current;
    if (seg) queueRef.current?.push(seg.push(chunk));
  }, []);

  const endReply = useCallback(() => {
    const seg = segmenterRef.current;
    segmenterRef.current = null;
    if (!seg) return;
    queueRef.current?.push(seg.flush());
    queueRef.current?.end();
  }, []);

  const speakText = useCallback((key: string, text: string) => {
    segmenterRef.current = null;
    const q = getQueue();
    q.begin(key);
    q.push(segmentForSpeech(text));
    q.end();
  }, [getQueue]);

  // Cleanup on unmount
  useEffect(() => () => { queueRef.current?.stop(); }, []);

  const isSpeaking = speech.status === 'loading' || speech.status === 'speaking';

  return {
    ttsEnabled, setTtsEnabled, speech, isSpeaking, unlock,
    beginReply, pushText, endReply, speakText, stop,
  };
}
