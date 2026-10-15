import { describe, it, expect, vi } from 'vitest';
import { onSpeechClip, emitSpeechClip } from './speech-clip-bus';

describe('speech-clip-bus', () => {
  const clip = new Blob(['x']);
  const el = {} as HTMLAudioElement;

  it('delivers to subscribers and stops after unsubscribe', () => {
    const l = vi.fn();
    const off = onSpeechClip(l);
    emitSpeechClip(clip, el);
    off();
    emitSpeechClip(clip, el);
    expect(l).toHaveBeenCalledTimes(1);
    expect(l).toHaveBeenCalledWith(clip, el);
  });

  it('a throwing listener does not block the others', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const good = vi.fn();
    const off1 = onSpeechClip(() => { throw new Error('boom'); });
    const off2 = onSpeechClip(good);
    expect(() => emitSpeechClip(clip, el)).not.toThrow();
    expect(good).toHaveBeenCalledTimes(1);
    off1(); off2(); warn.mockRestore();
  });
});
