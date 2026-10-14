/**
 * Safari 16.4+ Audio Session API (`navigator.audioSession.type`). No-op in
 * browsers without it.
 *
 * The type is page-global and sticky: whatever is set last stays in force
 * until changed. 'playback' blocks microphone capture on iOS, so anything
 * that sets it must hand back 'auto' when done (a call left it on 'playback'
 * and broke the plain mic button until reload).
 */
export type AudioSessionType = 'auto' | 'playback' | 'play-and-record';

export function setAudioSessionType(type: AudioSessionType): void {
  try {
    const session = (navigator as unknown as { audioSession?: { type: string } }).audioSession;
    if (session) session.type = type;
  } catch { /* unsupported value / browser */ }
}
