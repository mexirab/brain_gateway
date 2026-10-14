import { describe, it, expect } from 'vitest';
import {
  SpeechQueue, TTSUnavailableError, type SpeechPlayer, type SpeechState,
} from './speech-queue';

const tick = () => new Promise((r) => setTimeout(r, 0));

/** Synth whose calls resolve only when the test says so. */
function controllableSynth() {
  const calls: { text: string; resolve: (b: Blob) => void; reject: (e: Error) => void }[] = [];
  const synth = (text: string) =>
    new Promise<Blob>((resolve, reject) => { calls.push({ text, resolve, reject }); });
  return { calls, synth };
}

/** Player that records order and finishes each clip on demand. */
function controllablePlayer(failWith?: Error) {
  const played: string[] = [];
  let finish: (() => void) | null = null;
  let abort: ((e: Error) => void) | null = null;
  const player: SpeechPlayer = {
    async play(b) {
      if (failWith) throw failWith;
      played.push(await b.text());
      return new Promise<void>((res, rej) => { finish = res; abort = rej; });
    },
    stop() { abort?.(Object.assign(new Error('stopped'), { name: 'AbortError' })); },
  };
  return { played, player, end: () => finish?.() };
}

const blob = (t: string) => new Blob([t]);

describe('SpeechQueue', () => {
  it('plays in order with at most two synth requests ahead', async () => {
    const { calls, synth } = controllableSynth();
    const p = controllablePlayer();
    const states: SpeechState[] = [];
    const q = new SpeechQueue(synth, p.player, (s) => states.push(s));

    q.begin('m1');
    q.push(['a', 'b', 'c', 'd']);
    expect(calls.map((c) => c.text)).toEqual(['a', 'b']); // capped at 2
    q.end();

    calls[1].resolve(blob('b')); // out-of-order completion
    await tick();
    expect(p.played).toEqual([]); // still waiting on 'a'
    calls[0].resolve(blob('a'));
    await tick(); await tick();
    expect(p.played).toEqual(['a']);
    expect(calls).toHaveLength(2); // 'b' fetched, 'a' playing → no new fetch yet

    p.end(); await tick(); await tick();
    expect(p.played).toEqual(['a', 'b']);
    expect(calls.map((c) => c.text)).toEqual(['a', 'b', 'c']);

    calls[2].resolve(blob('c'));
    p.end(); await tick(); await tick();
    calls[3].resolve(blob('d'));
    await tick(); await tick();
    p.end(); await tick(); await tick();
    p.end(); await tick();
    expect(p.played).toEqual(['a', 'b', 'c', 'd']);
    expect(states.map((s) => s.status)).toEqual(['loading', 'speaking', 'speaking', 'speaking', 'speaking', 'idle']);
    expect(states[0].key).toBe('m1');
  });

  it('reports asleep on 503 with the message key', async () => {
    const q = new SpeechQueue(
      () => Promise.reject(new TTSUnavailableError()),
      controllablePlayer().player,
      (s) => states.push(s),
    );
    const states: SpeechState[] = [];
    q.begin('m7'); q.push(['hello there']); q.end();
    await tick(); await tick();
    expect(states.at(-1)).toEqual({ status: 'asleep', key: 'm7' });
  });

  it('reports blocked when the browser refuses play()', async () => {
    const notAllowed = Object.assign(new Error('nope'), { name: 'NotAllowedError' });
    const states: SpeechState[] = [];
    const q = new SpeechQueue(async () => blob('x'), controllablePlayer(notAllowed).player, (s) => states.push(s));
    q.begin('m2'); q.push(['hello there']);
    await tick(); await tick(); await tick();
    expect(states.at(-1)).toEqual({ status: 'blocked', key: 'm2' });
  });

  it('reports interrupted when the OS pauses playback', async () => {
    const paused = Object.assign(new Error('paused'), { name: 'MediaInterrupted' });
    const states: SpeechState[] = [];
    const q = new SpeechQueue(async () => blob('x'), controllablePlayer(paused).player, (s) => states.push(s));
    q.begin('m4'); q.push(['hello there']);
    await tick(); await tick(); await tick();
    expect(states.at(-1)).toEqual({ status: 'interrupted', key: 'm4' });
  });

  it('stop() aborts in-flight synth, silences the player and goes idle', async () => {
    const signals: AbortSignal[] = [];
    const p = controllablePlayer();
    const states: SpeechState[] = [];
    const q = new SpeechQueue(
      (_t, signal) => { signals.push(signal); return Promise.resolve(blob('x')); },
      p.player,
      (s) => states.push(s),
    );
    q.begin('m3'); q.push(['one sentence', 'two sentence', 'three']);
    await tick(); await tick();
    q.stop();
    await tick();
    expect(signals.every((s) => s.aborted)).toBe(true);
    expect(states.at(-1)).toEqual({ status: 'idle', key: null });
    q.push(['ignored']); // no session → no-op
    expect(signals).toHaveLength(2);
  });

  it('begin() for a new message cancels the previous one', async () => {
    const p = controllablePlayer();
    const q = new SpeechQueue(async (t) => blob(t), p.player, () => {});
    q.begin('old'); q.push(['first reply']);
    await tick(); await tick();
    q.begin('new'); q.push(['second reply']); q.end();
    await tick(); await tick(); await tick();
    expect(p.played).toEqual(['first reply', 'second reply']);
  });
});
