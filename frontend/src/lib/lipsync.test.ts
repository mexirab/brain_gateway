import { describe, it, expect } from 'vitest';
import { analyzeVisemes, sampleTrack, TRACK_FPS } from './lipsync';

const RATE = 22050;

function tone(hz: number, seconds: number, amp = 0.5): Float32Array {
  const out = new Float32Array(Math.round(RATE * seconds));
  for (let i = 0; i < out.length; i++) out[i] = amp * Math.sin((2 * Math.PI * hz * i) / RATE);
  return out;
}

function concat(...parts: Float32Array[]): Float32Array {
  const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0));
  let off = 0;
  for (const p of parts) { out.set(p, off); off += p.length; }
  return out;
}

describe('analyzeVisemes', () => {
  it('produces one frame per 1/TRACK_FPS seconds', () => {
    const track = analyzeVisemes(tone(500, 1), RATE);
    expect(track.fps).toBe(TRACK_FPS);
    expect(track.env.length).toBe(TRACK_FPS);
  });

  it('keeps the mouth closed for silence', () => {
    const track = analyzeVisemes(new Float32Array(RATE), RATE);
    const mid = sampleTrack(track, 0.5);
    expect(mid.aa + mid.ih + mid.ou).toBe(0);
  });

  it('maps low pitch to ou, mid to aa and high to ih', () => {
    const low = sampleTrack(analyzeVisemes(tone(320, 0.5), RATE), 0.25);
    const mid = sampleTrack(analyzeVisemes(tone(950, 0.5), RATE), 0.25);
    const high = sampleTrack(analyzeVisemes(tone(2700, 0.5), RATE), 0.25);
    expect(low.ou).toBeGreaterThan(low.aa);
    expect(low.ou).toBeGreaterThan(low.ih);
    expect(mid.aa).toBeGreaterThan(mid.ou);
    expect(mid.aa).toBeGreaterThan(mid.ih);
    expect(high.ih).toBeGreaterThan(high.ou);
    expect(high.ih).toBeGreaterThan(high.aa);
  });

  it('opens on sound and closes again after it stops', () => {
    const track = analyzeVisemes(concat(tone(900, 0.4), new Float32Array(RATE * 0.6)), RATE);
    const talking = sampleTrack(track, 0.2);
    const after = sampleTrack(track, 0.9);
    expect(talking.aa).toBeGreaterThan(0.3);
    expect(after.aa + after.ih + after.ou).toBe(0);
  });

  it('normalises loudness per clip (quiet TTS still opens the mouth)', () => {
    const quiet = sampleTrack(analyzeVisemes(tone(900, 0.5, 0.02), RATE), 0.25);
    expect(quiet.aa).toBeGreaterThan(0.3);
  });
});

describe('analyzeVisemes edge cases', () => {
  it('keeps the mouth closed for hiss-only clips (no normalising noise up)', () => {
    const hiss = Float32Array.from({ length: RATE }, () => (Math.random() * 2 - 1) * 0.0005);
    const track = analyzeVisemes(hiss, RATE);
    expect(Math.max(...track.aa, ...track.ih, ...track.ou)).toBe(0);
  });

  it('closes between words separated by a 150 ms gap', () => {
    const track = analyzeVisemes(concat(tone(900, 0.3), new Float32Array(RATE * 0.15), tone(900, 0.3)), RATE);
    // Some frame inside the gap (0.30–0.45 s) must be fully closed.
    const gap = [];
    for (let t = 0.3; t < 0.45; t += 1 / TRACK_FPS) {
      const f = sampleTrack(track, t);
      gap.push(f.aa + f.ih + f.ou);
    }
    expect(Math.min(...gap)).toBe(0);
  });

  it('holds the vowel shape while releasing instead of morphing', () => {
    const noisyTail = Float32Array.from({ length: RATE * 0.3 }, () => (Math.random() * 2 - 1) * 0.001);
    const track = analyzeVisemes(concat(tone(320, 0.3), noisyTail), RATE);
    for (let f = 15; f < track.env.length; f++) {
      expect(track.ih[f]).toBeLessThanOrEqual(track.ou[f]);
    }
  });

  it('opens for quiet speech after a loud first word', () => {
    const track = analyzeVisemes(concat(tone(900, 0.2, 0.9), tone(900, 0.6, 0.2)), RATE);
    expect(sampleTrack(track, 0.6).aa).toBeGreaterThan(0.3);
  });

  it('handles empty and sub-frame clips at other sample rates', () => {
    expect(analyzeVisemes(new Float32Array(0), 44100).env.length).toBe(1);
    const tiny = analyzeVisemes(tone(900, 0.005), 44100);
    expect(tiny.env.length).toBe(1);
    expect(Number.isFinite(tiny.aa[0])).toBe(true);
  });
});

describe('sampleTrack', () => {
  it('is closed before, after, and without a track', () => {
    const track = analyzeVisemes(tone(900, 0.5), RATE);
    for (const f of [sampleTrack(track, -1), sampleTrack(track, 5), sampleTrack(track, track.env.length / track.fps), sampleTrack(null, 0.1), sampleTrack(track, NaN)]) {
      expect(f).toEqual({ aa: 0, ih: 0, ou: 0, env: 0 });
    }
  });
});
