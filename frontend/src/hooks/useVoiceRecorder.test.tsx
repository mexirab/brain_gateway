import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { STTUnavailableError } from '@/lib/stt';
import useVoiceRecorder, {
  LEFT_PAGE, MAX_RECORDING_MS, MIC_MUTED, MIC_OPEN_FAILED, MIC_OPEN_TIMEOUT_MS, NOT_FINISHED,
  NOTHING_HEARD, NOTHING_RECORDED, STOPPED_EARLY, STOP_TIMEOUT_MS, TOO_LONG,
} from './useVoiceRecorder';

const transcribe = vi.hoisted(() => vi.fn<(blob: Blob) => Promise<string>>());
vi.mock('@/lib/stt', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/stt')>()),
  transcribe,
}));

/** Everything the fakes saw, in order: session types, getUserMedia, recorder.stop, uploads. */
let events: string[];

class FakeRecorder {
  static last: FakeRecorder | null = null;
  static isTypeSupported(mime: string) { return mime === 'audio/mp4'; }
  state: 'inactive' | 'recording' = 'inactive';
  mimeType: string;
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  /** What the final dataavailable carries; null = no data event at all. */
  data: Blob | null = new Blob(['audio-bytes'], { type: 'audio/mp4' });
  /** false = the stop event never comes (a browser bug the hook must survive). */
  firesStop = true;

  constructor(_stream: MediaStream, opts?: { mimeType?: string }) {
    this.mimeType = opts?.mimeType ?? '';
    FakeRecorder.last = this;
  }

  start() { this.state = 'recording'; }

  stop() {
    if (this.state === 'inactive') throw new DOMException('not recording', 'InvalidStateError');
    events.push('recorder.stop');
    this.finish();
  }

  /** The browser ends the recording itself (audio-session change, phone call…). */
  browserStop() { this.finish(); }

  private finish() {
    this.state = 'inactive';
    if (!this.firesStop) return;
    setTimeout(() => {
      if (this.data) this.ondataavailable?.({ data: this.data });
      this.onstop?.();
    }, 0);
  }
}

interface FakeTrack {
  stop: ReturnType<typeof vi.fn>;
  readyState: string;
  muted: boolean;
  onmute: (() => void) | null;
  onunmute: (() => void) | null;
  getSettings: () => MediaTrackSettings;
}

let track: FakeTrack;
let getUserMedia: ReturnType<typeof vi.fn>;
const nav = navigator as unknown as { audioSession?: { type: string }; mediaDevices?: unknown };
const fakeStream = () => ({ getTracks: () => [track], getAudioTracks: () => [track] });

beforeEach(() => {
  events = [];
  transcribe.mockReset();
  transcribe.mockImplementation(async () => { events.push('transcribe'); return 'hello'; });
  FakeRecorder.last = null;
  track = {
    stop: vi.fn(() => { events.push('track.stop'); track.readyState = 'ended'; }),
    readyState: 'live',
    muted: false,
    onmute: null,
    onunmute: null,
    getSettings: () => ({ echoCancellation: true, sampleRate: 48000, channelCount: 1 }),
  };
  getUserMedia = vi.fn(async (constraints: MediaStreamConstraints) => {
    events.push(`getUserMedia:${JSON.stringify(constraints)}`);
    return fakeStream();
  });
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true });
  let type = 'auto';
  nav.audioSession = {
    get type() { return type; },
    set type(t: string) { type = t; events.push(`session:${t}`); },
  };
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete nav.audioSession;
  delete nav.mediaDevices;
});

async function started() {
  const hook = renderHook(() => useVoiceRecorder());
  await act(async () => { await hook.result.current.startRecording(); });
  expect(hook.result.current.isRecording).toBe(true);
  return hook;
}

async function stop(hook: Awaited<ReturnType<typeof started>>): Promise<string | null> {
  let out: string | null = 'unset';
  await act(async () => { out = await hook.result.current.stopRecording(); });
  return out;
}

/** A pending getUserMedia the test resolves by hand. */
function slowMic(): () => void {
  let release: () => void = () => {};
  getUserMedia.mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve(fakeStream()); }));
  return () => release();
}

const consoleText = () => vi.mocked(console.warn).mock.calls.map((c) => c.join(' ')).join('\n');

describe('useVoiceRecorder', () => {
  it('opens the mic the way call mode does, and hands the session back before the upload', async () => {
    const hook = await started();
    expect(events).toEqual([
      'session:play-and-record', // before the mic opens
      'getUserMedia:{"audio":{"echoCancellation":true,"noiseSuppression":true,"autoGainControl":true}}',
    ]);
    expect(await stop(hook)).toBe('hello');
    // The reply must never arrive under play-and-record.
    expect(events.slice(2)).toEqual(['recorder.stop', 'track.stop', 'session:auto', 'transcribe']);
    expect(transcribe.mock.calls[0][0].size).toBeGreaterThan(0);
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.isRecording).toBe(false);
  });

  it('logs numbers only: open delay, settings, duration, size, track state, transcript length', async () => {
    transcribe.mockResolvedValue('turn on the lamp');
    const hook = await started();
    await stop(hook);
    const text = consoleText();
    expect(text).toMatch(/\[mic\] recording \d+ ms after the tap; session play-and-record\/n\/a, echo on, noise \?, gain \?, 48000 Hz, 1 ch/);
    expect(text).toMatch(/\[mic\] recorded \d+ ms, 11 bytes, audio\/mp4; track live, mutes 0/);
    expect(text).toContain('[mic] heard 16 characters');
    expect(text).not.toContain('lamp');
  });

  it('stops the recorder synchronously, before its first await', async () => {
    const hook = await started();
    await act(async () => {
      const pending = hook.result.current.stopRecording();
      events.push('after-call'); // e.g. the reply-audio unlock in the same tap
      await pending;
    });
    expect(events.indexOf('recorder.stop')).toBeLessThan(events.indexOf('after-call'));
  });

  it('says so when nothing was heard instead of doing nothing', async () => {
    transcribe.mockResolvedValue('');
    const hook = await started();
    expect(await stop(hook)).toBeNull();
    expect(hook.result.current.error).toBe(NOTHING_HEARD);
  });

  it('names a muted mic when nothing was heard after the phone muted it', async () => {
    transcribe.mockResolvedValue('');
    const hook = await started();
    act(() => { track.muted = true; track.onmute?.(); });
    expect(await stop(hook)).toBeNull();
    expect(hook.result.current.error).toBe(MIC_MUTED);
    expect(consoleText()).toMatch(/microphone muted by the browser at \d+ ms/);
    expect(consoleText()).toMatch(/track live \(muted now\), mutes 1/);
    expect(track.onmute).toBeNull(); // handlers detached on release
  });

  it('does not upload an empty recording, and says nothing was recorded', async () => {
    const hook = await started();
    FakeRecorder.last!.data = null;
    expect(await stop(hook)).toBeNull();
    expect(transcribe).not.toHaveBeenCalled();
    expect(hook.result.current.error).toBe(NOTHING_RECORDED);
    expect(events).toContain('session:auto');
  });

  it('drops a recording the browser ended on its own, and releases the mic', async () => {
    const hook = await started();
    await act(async () => {
      FakeRecorder.last!.browserStop();
      await new Promise((r) => setTimeout(r, 5));
    });
    expect(hook.result.current.isRecording).toBe(false);
    expect(hook.result.current.error).toBe(STOPPED_EARLY);
    expect(events.slice(-2)).toEqual(['track.stop', 'session:auto']);
    expect(await stop(hook)).toBeNull(); // nothing left to stop
    expect(transcribe).not.toHaveBeenCalled();
  });

  it('still transcribes a recording the browser finished just before the stop tap', async () => {
    const hook = await started();
    FakeRecorder.last!.browserStop(); // state is inactive; the stop event is still queued
    expect(await stop(hook)).toBe('hello');
    expect(events).not.toContain('recorder.stop');
    expect(hook.result.current.error).toBeNull();
  });

  it('drops the recording when the page is hidden', async () => {
    const hook = await started();
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    delete (document as unknown as { visibilityState?: string }).visibilityState; // back to jsdom's own
    expect(hook.result.current.isRecording).toBe(false);
    expect(hook.result.current.error).toBe(LEFT_PAGE);
    expect(events).toContain('recorder.stop');
    expect(events.slice(-2)).toEqual(['track.stop', 'session:auto']);
    await act(async () => { await new Promise((r) => setTimeout(r, 5)); }); // the stop event
    expect(transcribe).not.toHaveBeenCalled();
    expect(hook.result.current.error).toBe(LEFT_PAGE); // not overwritten by the late stop event
  });

  it('drops a recording left running past the time limit', async () => {
    vi.useFakeTimers();
    const hook = await started();
    await act(async () => { await vi.advanceTimersByTimeAsync(MAX_RECORDING_MS); });
    expect(hook.result.current.isRecording).toBe(false);
    expect(hook.result.current.error).toBe(TOO_LONG);
    expect(transcribe).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalled();
  });

  it('a stop before the limit cancels it', async () => {
    vi.useFakeTimers();
    const hook = await started();
    let out: string | null = 'unset';
    await act(async () => {
      const p = hook.result.current.stopRecording().then((v) => { out = v; });
      await vi.advanceTimersByTimeAsync(10);
      await p;
    });
    expect(out).toBe('hello');
    await act(async () => { await vi.advanceTimersByTimeAsync(MAX_RECORDING_MS); });
    expect(hook.result.current.error).toBeNull();
  });

  it('does not hang when the stop event never comes', async () => {
    vi.useFakeTimers();
    const hook = await started();
    FakeRecorder.last!.firesStop = false; // WebKit delivers the only chunk with the stop event
    let out: string | null = 'unset';
    await act(async () => {
      const p = hook.result.current.stopRecording().then((v) => { out = v; });
      await vi.advanceTimersByTimeAsync(STOP_TIMEOUT_MS);
      await p;
    });
    expect(out).toBeNull();
    expect(hook.result.current.error).toBe(NOT_FINISHED);
    expect(hook.result.current.isRecording).toBe(false);
    expect(transcribe).not.toHaveBeenCalled();
    expect(events).toContain('session:auto');
  });

  it('shows that the mic is opening, and opens it once when tapped twice', async () => {
    const release = slowMic();
    const hook = renderHook(() => useVoiceRecorder());
    let first: Promise<void> | undefined;
    act(() => { first = hook.result.current.startRecording(); });
    expect(hook.result.current.isStarting).toBe(true);
    await act(async () => {
      await hook.result.current.startRecording();
      release();
      await first;
    });
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(hook.result.current.isStarting).toBe(false);
    expect(hook.result.current.isRecording).toBe(true);
  });

  it('gives up on a mic that never opens, and closes it if it opens later', async () => {
    vi.useFakeTimers();
    const release = slowMic();
    const hook = renderHook(() => useVoiceRecorder());
    await act(async () => {
      const p = hook.result.current.startRecording();
      await vi.advanceTimersByTimeAsync(MIC_OPEN_TIMEOUT_MS);
      await p;
    });
    expect(hook.result.current.isStarting).toBe(false);
    expect(hook.result.current.isRecording).toBe(false);
    expect(hook.result.current.error).toBe(MIC_OPEN_FAILED);
    expect(events.at(-1)).toBe('session:auto');
    await act(async () => { release(); await vi.advanceTimersByTimeAsync(1); });
    expect(track.stop).toHaveBeenCalled();
    expect(FakeRecorder.last).toBeNull();
  });

  it('explains a blocked mic and leaves the session on auto', async () => {
    getUserMedia.mockRejectedValueOnce(new DOMException('denied', 'NotAllowedError'));
    const hook = renderHook(() => useVoiceRecorder());
    await act(async () => { await hook.result.current.startRecording(); });
    expect(hook.result.current.isRecording).toBe(false);
    expect(hook.result.current.error).toMatch(/blocked/);
    expect(events.at(-1)).toBe('session:auto');
  });

  it('releases the mic when the chat unmounts mid-recording', async () => {
    const hook = await started();
    hook.unmount();
    expect(events).toContain('recorder.stop');
    expect(events.slice(-2)).toEqual(['track.stop', 'session:auto']);
  });

  it('closes a mic that opened after the chat unmounted', async () => {
    const release = slowMic();
    const hook = renderHook(() => useVoiceRecorder());
    let starting: Promise<void> | undefined;
    act(() => { starting = hook.result.current.startRecording(); });
    hook.unmount();
    release();
    await starting;
    expect(track.stop).toHaveBeenCalled();
    expect(FakeRecorder.last).toBeNull();
    expect(events.at(-1)).toBe('session:auto');
  });

  it('sends nothing when the chat unmounts during transcription', async () => {
    let finish: (t: string) => void = () => {};
    transcribe.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const hook = await started();
    let pending: Promise<string | null> | undefined;
    await act(async () => {
      pending = hook.result.current.stopRecording();
      await new Promise((r) => setTimeout(r, 5));
    });
    hook.unmount();
    finish('too late');
    expect(await pending).toBeNull();
  });

  it('drops a mic that finished opening on a hidden page', async () => {
    const release = slowMic();
    const hook = renderHook(() => useVoiceRecorder());
    let starting: Promise<void> | undefined;
    act(() => { starting = hook.result.current.startRecording(); });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    try {
      await act(async () => { release(); await starting; });
    } finally {
      delete (document as unknown as { visibilityState?: string }).visibilityState;
    }
    expect(hook.result.current.isRecording).toBe(false);
    expect(hook.result.current.error).toBe(LEFT_PAGE);
    expect(FakeRecorder.last).toBeNull();
    expect(track.stop).toHaveBeenCalled();
    expect(events.at(-1)).toBe('session:auto');
  });

  it('does not blame a mute that came after the stop tap', async () => {
    transcribe.mockResolvedValue('');
    const hook = await started();
    await act(async () => {
      const pending = hook.result.current.stopRecording();
      track.onmute?.(); // e.g. while the reply-audio unlock plays
      await pending;
    });
    expect(hook.result.current.error).toBe(NOTHING_HEARD);
    expect(consoleText()).toMatch(/muted by the browser at \d+ ms, after stop/);
  });

  it('keeps the screen awake while recording, and lets go after', async () => {
    const release = vi.fn(async () => { events.push('wakelock.release'); });
    const request = vi.fn(async () => { events.push('wakelock.request'); return { release }; });
    Object.defineProperty(navigator, 'wakeLock', { value: { request }, configurable: true });
    try {
      const hook = await started();
      expect(events[0]).toBe('wakelock.request'); // inside the tap, before the mic opens
      expect(release).not.toHaveBeenCalled();
      await stop(hook);
      expect(release).toHaveBeenCalledTimes(1);

      getUserMedia.mockRejectedValueOnce(new DOMException('denied', 'NotAllowedError'));
      await act(async () => { await hook.result.current.startRecording(); });
      expect(request).toHaveBeenCalledTimes(2);
      expect(release).toHaveBeenCalledTimes(2); // a failed start lets go too
    } finally {
      delete (navigator as unknown as { wakeLock?: unknown }).wakeLock;
    }
  });

  it('says Helios is asleep on a 503, and logs only the error', async () => {
    transcribe.mockRejectedValue(new STTUnavailableError());
    const hook = await started();
    expect(await stop(hook)).toBeNull();
    expect(hook.result.current.error).toMatch(/asleep/);
    expect(consoleText()).toContain('[mic] transcribe failed: STT unavailable');
  });
});
