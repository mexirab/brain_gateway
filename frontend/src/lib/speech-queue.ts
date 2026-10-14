/**
 * Ordered, sentence-at-a-time speech playback with bounded TTS prefetch.
 *
 * Segments are synthesized ahead of playback but never more than
 * `maxInFlight` beyond what has finished playing (the TTS server is a single
 * GPU), and always played in push order. Failures end the session with a
 * status the UI can act on instead of failing silently:
 *   asleep  — TTS endpoint answered 503 (Helios powered down)
 *   blocked — the browser refused play() (iOS autoplay: NotAllowedError)
 *   interrupted — the OS paused playback (screen lock, call, Siri, Control
 *                 Center); the player rejects with a MediaInterrupted error
 *   error   — anything else
 *
 * Pure TypeScript with injected synth/player so it is unit-testable.
 */

export type SpeechStatus =
  | 'idle' | 'loading' | 'speaking' | 'blocked' | 'asleep' | 'interrupted' | 'error';

export interface SpeechState {
  status: SpeechStatus;
  /** Which message the status belongs to (null when idle). */
  key: string | null;
}

/** Thrown by a synth function when the TTS service is down (HTTP 503). */
export class TTSUnavailableError extends Error {
  constructor() {
    super('TTS unavailable');
    this.name = 'TTSUnavailableError';
  }
}

export type SynthFn = (text: string, signal: AbortSignal) => Promise<Blob>;

export interface SpeechPlayer {
  /** Resolves when the clip finishes; rejects if playback fails or is stopped. */
  play(audio: Blob): Promise<void>;
  stop(): void;
}

interface Session {
  key: string;
  texts: string[];
  fetches: Promise<Blob>[];
  played: number;
  ended: boolean;
  controller: AbortController;
  wake: (() => void) | null;
}

export class SpeechQueue {
  private session: Session | null = null;

  constructor(
    private readonly synth: SynthFn,
    private readonly player: SpeechPlayer,
    private readonly onState: (s: SpeechState) => void,
    private readonly maxInFlight = 2,
  ) {}

  /** Start a new utterance for `key`, cancelling any current one. */
  begin(key: string): void {
    this.cancel();
    const s: Session = {
      key,
      texts: [],
      fetches: [],
      played: 0,
      ended: false,
      controller: new AbortController(),
      wake: null,
    };
    this.session = s;
    void this.run(s);
  }

  push(texts: string[]): void {
    const s = this.session;
    if (!s || s.ended || texts.length === 0) return;
    s.texts.push(...texts);
    this.pump(s);
    this.wakeUp(s);
  }

  /** No more segments will be pushed for the current utterance. */
  end(): void {
    const s = this.session;
    if (!s) return;
    s.ended = true;
    this.wakeUp(s);
  }

  stop(): void {
    const had = this.session !== null;
    this.cancel();
    if (had) this.onState({ status: 'idle', key: null });
  }

  private cancel(): void {
    const s = this.session;
    if (!s) return;
    this.session = null;
    s.controller.abort();
    this.player.stop();
    this.wakeUp(s);
  }

  private wakeUp(s: Session): void {
    const w = s.wake;
    s.wake = null;
    w?.();
  }

  private pump(s: Session): void {
    while (s.fetches.length < s.texts.length && s.fetches.length - s.played < this.maxInFlight) {
      const p = this.synth(s.texts[s.fetches.length], s.controller.signal);
      p.catch(() => {}); // observed in run(); avoid unhandled-rejection noise
      s.fetches.push(p);
    }
  }

  private async run(s: Session): Promise<void> {
    const live = () => this.session === s;
    const finish = (status: SpeechStatus) => {
      if (!live()) return;
      this.session = null;
      s.controller.abort();
      this.onState(status === 'idle' ? { status, key: null } : { status, key: s.key });
    };

    for (let i = 0; ; i++) {
      while (i >= s.texts.length) {
        if (s.ended) return finish('idle');
        await new Promise<void>((resolve) => { s.wake = resolve; });
        if (!live()) return;
      }
      this.pump(s);
      if (i === 0) this.onState({ status: 'loading', key: s.key });

      let audio: Blob;
      try {
        audio = await s.fetches[i];
      } catch (err) {
        return finish(err instanceof TTSUnavailableError ? 'asleep' : 'error');
      }
      if (!live()) return;

      this.onState({ status: 'speaking', key: s.key });
      try {
        await this.player.play(audio);
      } catch (err) {
        if (!live()) return;
        const name = err instanceof Error ? err.name : '';
        return finish(
          name === 'NotAllowedError' ? 'blocked' : name === 'MediaInterrupted' ? 'interrupted' : 'error',
        );
      }
      if (!live()) return;
      s.played = i + 1;
      this.pump(s);
    }
  }
}
