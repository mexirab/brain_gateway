/** Speech-to-text helpers shared by the mic button and hands-free call mode. */

const PROXY = '/api/proxy';

/** Thrown when the STT service is down (HTTP 503 — usually Helios asleep). */
export class STTUnavailableError extends Error {
  constructor() {
    super('STT unavailable');
    this.name = 'STTUnavailableError';
  }
}

/**
 * Best recording format this browser supports. Safari before 18.4 has no
 * webm in MediaRecorder; the STT server decodes anything via ffmpeg.
 */
export function pickRecorderMime(): { mimeType?: string; ext: string } {
  const candidates: [string, string][] = [
    ['audio/webm;codecs=opus', 'webm'],
    ['audio/webm', 'webm'],
    ['audio/mp4', 'm4a'],
  ];
  if (typeof MediaRecorder !== 'undefined' && typeof MediaRecorder.isTypeSupported === 'function') {
    for (const [mime, ext] of candidates) {
      if (MediaRecorder.isTypeSupported(mime)) return { mimeType: mime, ext };
    }
  }
  return { ext: 'webm' }; // browser default
}

export function extForMime(mime: string): string {
  return mime.includes('mp4') || mime.includes('aac') ? 'm4a' : 'webm';
}

/** POST a recording to STT. Returns the transcript ('' when nothing was heard). */
export async function transcribe(blob: Blob, signal?: AbortSignal): Promise<string> {
  const form = new FormData();
  form.append('file', blob, `recording.${extForMime(blob.type)}`);
  const res = await fetch(`${PROXY}/api/stt/transcribe`, { method: 'POST', body: form, signal });
  if (res.status === 503) throw new STTUnavailableError();
  if (!res.ok) throw new Error(`STT ${res.status}`);
  const data = await res.json();
  return typeof data?.text === 'string' ? data.text.trim() : '';
}
