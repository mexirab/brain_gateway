/**
 * Offline lip-sync for the chat avatar: analyse a decoded TTS clip once into
 * a per-frame viseme track, then sample it with the <audio> element's
 * currentTime while it plays. Done offline (not with a live AnalyserNode) so
 * the reply audio never has to be routed through Web Audio — on iOS that
 * changes how the shared element plays and can silence it.
 *
 * Mapping (ported from jess-face): spectral centroid picks the vowel shape
 * (low → ou, mid → aa, high → ih), loudness picks how far the mouth opens.
 */

export interface VisemeFrame { aa: number; ih: number; ou: number; env: number }

export interface VisemeTrack {
  fps: number;
  aa: Float32Array;
  ih: Float32Array;
  ou: Float32Array;
  /** 0..1 loudness envelope (drives head nod + glow). */
  env: Float32Array;
}

export const TRACK_FPS = 50;
const WINDOW = 1024;
const MIN_HZ = 80;
const MAX_HZ = 6500;
const SILENCE = 0.045;
// Absolute floor (sum of FFT magnitudes over the speech band) below which a
// frame counts as silence however quiet the whole clip is — otherwise
// per-clip normalisation would scale hiss/clicks up to full mouth movement.
const ABS_FLOOR = 2;
// Envelope release per frame (50 fps): 0.55 closes in ~100 ms, so the mouth
// shuts between words instead of hovering half-open.
const RELEASE = 0.55;
const GAIN = 1.15;

const HANN = Float32Array.from({ length: WINDOW }, (_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (WINDOW - 1)));

/** In-place iterative radix-2 FFT (re/im length must be a power of two). */
function fft(re: Float32Array, im: Float32Array): void {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      let tmp = re[i]; re[i] = re[j]; re[j] = tmp;
      tmp = im[i]; im[i] = im[j]; im[j] = tmp;
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wr = Math.cos(ang), wi = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let cr = 1, ci = 0;
      for (let k = 0; k < len / 2; k++) {
        const a = i + k, b = a + len / 2;
        const tr = re[b] * cr - im[b] * ci;
        const ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr; im[b] = im[a] - ti;
        re[a] += tr; im[a] += ti;
        const ncr = cr * wr - ci * wi;
        ci = cr * wi + ci * wr;
        cr = ncr;
      }
    }
  }
}

const gauss = (lc: number, peakHz: number) => Math.exp(-(((lc - Math.log2(peakHz)) / 1.05) ** 2));

export function analyzeVisemes(samples: Float32Array, sampleRate: number): VisemeTrack {
  const hop = Math.max(1, Math.round(sampleRate / TRACK_FPS));
  const frames = Math.max(1, Math.ceil(samples.length / hop));
  const track: VisemeTrack = {
    fps: TRACK_FPS,
    aa: new Float32Array(frames), ih: new Float32Array(frames),
    ou: new Float32Array(frames), env: new Float32Array(frames),
  };
  const re = new Float32Array(WINDOW);
  const im = new Float32Array(WINDOW);
  const binHz = sampleRate / WINDOW;
  const lo = Math.max(1, Math.round(MIN_HZ / binHz));
  const hi = Math.min(WINDOW / 2 - 1, Math.round(MAX_HZ / binHz));

  // First pass: raw loudness + centroid per frame.
  const loud = new Float32Array(frames);
  const cent = new Float32Array(frames);
  let peak = 0;
  for (let f = 0; f < frames; f++) {
    const start = f * hop - WINDOW / 2;
    for (let i = 0; i < WINDOW; i++) {
      re[i] = (samples[start + i] ?? 0) * HANN[i];
      im[i] = 0;
    }
    fft(re, im);
    let sum = 0, wsum = 0;
    for (let i = lo; i <= hi; i++) {
      const mag = Math.hypot(re[i], im[i]);
      sum += mag;
      wsum += mag * i * binHz;
    }
    loud[f] = sum;
    cent[f] = sum > 0 ? wsum / sum : 0;
    if (sum > peak) peak = sum;
  }

  // Second pass: normalise loudness per clip (TTS level varies by voice/
  // engine) against a typical voiced level — not the single loudest frame,
  // which left the mouth barely moving after a loud first word — and turn it
  // into mouth shapes. Attack fast, release slower.
  const voiced = Array.from(loud).filter((v) => v > Math.max(peak * 0.05, ABS_FLOOR)).sort((a, b) => a - b);
  if (!voiced.length) return track; // silence / hiss only → mouth stays closed
  const ref = voiced[Math.floor(voiced.length * 0.65)];
  let env = 0;
  // Vowel shape of the last voiced frame: held while the envelope releases,
  // so the mouth closes on the same shape instead of morphing in the gap.
  let shapeCent = 0;
  for (let f = 0; f < frames; f++) {
    const isVoiced = loud[f] > ABS_FLOOR && cent[f] > 0;
    const vol = isVoiced ? Math.min(1, loud[f] / ref) : 0;
    if (isVoiced && vol >= SILENCE) shapeCent = cent[f];
    env = Math.max(env * RELEASE, vol);
    track.env[f] = env;
    if (env < SILENCE || shapeCent === 0) continue;
    const lc = Math.log2(Math.max(120, shapeCent));
    const wOU = gauss(lc, 320), wAA = gauss(lc, 950), wIH = gauss(lc, 2700);
    const wT = wOU + wAA + wIH || 1;
    const open = Math.min(1, env * GAIN);
    track.ou[f] = (wOU / wT) * open * 0.85;
    track.aa[f] = (wAA / wT) * open * 0.95;
    track.ih[f] = (wIH / wT) * open * 0.65;
  }
  return track;
}

const CLOSED: VisemeFrame = { aa: 0, ih: 0, ou: 0, env: 0 };

/** Mouth shape at playback time `t` seconds (closed outside the clip). */
export function sampleTrack(track: VisemeTrack | null, t: number): VisemeFrame {
  if (!track || !(t >= 0)) return CLOSED;
  const f = Math.floor(t * track.fps);
  if (f >= track.env.length) return CLOSED;
  return { aa: track.aa[f], ih: track.ih[f], ou: track.ou[f], env: track.env[f] };
}

// One decode-only context, reused (Safari has historically capped contexts).
let decodeCtx: OfflineAudioContext | null = null;

/**
 * Decode a clip and return its first channel (TTS is mono). Rejects if the
 * browser can't decode it — including the legacy callback-only
 * webkitOfflineAudioContext, which the avatar treats like any decode failure.
 */
export async function decodeClip(clip: Blob): Promise<{ samples: Float32Array; sampleRate: number }> {
  if (!decodeCtx) {
    const Ctor = (window as unknown as { OfflineAudioContext?: typeof OfflineAudioContext }).OfflineAudioContext;
    if (!Ctor) throw new Error('no OfflineAudioContext');
    decodeCtx = new Ctor(1, 1, 22050);
  }
  const buf = await decodeCtx.decodeAudioData(await clip.arrayBuffer());
  return { samples: buf.getChannelData(0), sampleRate: buf.sampleRate };
}
