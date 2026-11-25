import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import ChatInput from './ChatInput';

const transcribe = vi.hoisted(() => vi.fn<(blob: Blob) => Promise<string>>());
vi.mock('@/lib/stt', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/stt')>()),
  transcribe,
}));

let events: string[];

class FakeRecorder {
  static isTypeSupported() { return false; }
  state = 'inactive';
  mimeType = 'audio/mp4';
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  start() { this.state = 'recording'; }
  stop() {
    events.push('recorder.stop');
    this.state = 'inactive';
    setTimeout(() => {
      this.ondataavailable?.({ data: new Blob(['audio']) });
      this.onstop?.();
    }, 0);
  }
}

beforeEach(() => {
  events = [];
  transcribe.mockReset();
  const track = { stop: () => {} };
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: async () => ({ getTracks: () => [track], getAudioTracks: () => [track] }) },
    configurable: true,
  });
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (navigator as unknown as { mediaDevices?: unknown }).mediaDevices;
});

const settle = () => new Promise((r) => setTimeout(r, 5));

async function startRecording() {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Voice input' })); });
  expect(screen.getByRole('button', { name: 'Stop recording' })).toBeEnabled();
}

describe('ChatInput mic button', () => {
  it('unlocks reply audio inside the stop tap, after the recorder stopped, and sends the transcript', async () => {
    transcribe.mockResolvedValue('add milk to the list');
    const onSend = vi.fn();
    const onUserGesture = vi.fn(() => events.push('unlock'));
    const onRecordStart = vi.fn();
    render(<ChatInput onSend={onSend} onUserGesture={onUserGesture} onRecordStart={onRecordStart} />);

    await startRecording();
    expect(onRecordStart).toHaveBeenCalledTimes(1);
    expect(onUserGesture).not.toHaveBeenCalled(); // nothing plays while the mic opens

    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Stop recording' }));
      events.push('tap-handler-returned'); // iOS only honours an unlock before this point
    });
    expect(events).toEqual(['recorder.stop', 'unlock', 'tap-handler-returned']);
    await act(settle);
    expect(onSend).toHaveBeenCalledWith('add milk to the list', 'voice', []);
  });

  it('never greys out stop while recording, even if the input gets disabled', async () => {
    transcribe.mockResolvedValue('');
    const { rerender } = render(<ChatInput onSend={vi.fn()} />);
    await startRecording();
    rerender(<ChatInput onSend={vi.fn()} disabled />);
    const stop = screen.getByRole('button', { name: 'Stop recording' });
    expect(stop).toBeEnabled();
    await act(async () => { fireEvent.click(stop); await settle(); });
    expect(events).toEqual(['recorder.stop']);
  });

  it('does not send a typed draft while the mic records', async () => {
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'typed before the mic' } });
    await startRecording();
    const send = screen.getByRole('button', { name: 'Send message' });
    expect(send).toBeDisabled();
    fireEvent.click(send);
    expect(onSend).not.toHaveBeenCalled();
  });

  it('keeps the call button disabled while the mic opens', async () => {
    let open: () => void = () => {};
    const track = { stop: () => {} };
    Object.defineProperty(navigator, 'mediaDevices', {
      value: { getUserMedia: () => new Promise((resolve) => { open = () => resolve({ getTracks: () => [track], getAudioTracks: () => [track] }); }) },
      configurable: true,
    });
    render(<ChatInput onSend={vi.fn()} onStartCall={vi.fn()} />);
    const call = screen.getByRole('button', { name: 'Start hands-free conversation' });
    expect(call).toBeEnabled();
    act(() => { fireEvent.click(screen.getByRole('button', { name: 'Voice input' })); });
    const mic = screen.getByRole('button', { name: 'Opening the mic' });
    expect(mic).toHaveAttribute('aria-busy', 'true');
    expect(mic).toBeEnabled(); // keeps keyboard focus; the hook ignores a second tap
    expect(call).toBeDisabled();
    await act(async () => { open(); await settle(); });
    expect(screen.getByRole('button', { name: 'Stop recording' })).toBeEnabled();
    expect(call).toBeDisabled();
  });

  it('shows a message when nothing was heard', async () => {
    transcribe.mockResolvedValue('');
    const onSend = vi.fn();
    render(<ChatInput onSend={onSend} />);
    await startRecording();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Stop recording' }));
      await settle();
    });
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Didn’t catch anything');
  });
});
