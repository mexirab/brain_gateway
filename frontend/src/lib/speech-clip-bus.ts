/**
 * Tiny pub/sub that tells listeners (the chat avatar) when a TTS clip starts
 * playing on the shared reply <audio> element. Listeners read the blob and
 * the element's currentTime; they must never touch playback itself.
 */
export type SpeechClipListener = (clip: Blob, el: HTMLAudioElement) => void;

const listeners = new Set<SpeechClipListener>();

export function onSpeechClip(listener: SpeechClipListener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function emitSpeechClip(clip: Blob, el: HTMLAudioElement): void {
  listeners.forEach((l) => {
    try {
      l(clip, el);
    } catch (err) {
      // a broken listener must not break speech
      console.warn('[speech-clip-bus] listener failed', err);
    }
  });
}
