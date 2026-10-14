import { describe, it, expect, afterEach } from 'vitest';
import { setAudioSessionType } from './audio-session';

const nav = navigator as unknown as { audioSession?: { type: string } };

describe('setAudioSessionType', () => {
  afterEach(() => { delete nav.audioSession; });

  it('sets the type when the Audio Session API exists', () => {
    nav.audioSession = { type: 'auto' };
    setAudioSessionType('playback');
    expect(nav.audioSession.type).toBe('playback');
    setAudioSessionType('auto');
    expect(nav.audioSession.type).toBe('auto');
  });

  it('is a no-op without the API and never throws', () => {
    expect(() => setAudioSessionType('play-and-record')).not.toThrow();
    nav.audioSession = Object.defineProperty({} as { type: string }, 'type', {
      set() { throw new TypeError('unsupported'); },
    });
    expect(() => setAudioSessionType('auto')).not.toThrow();
  });
});
