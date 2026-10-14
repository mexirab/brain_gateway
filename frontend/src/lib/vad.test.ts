import { describe, it, expect } from 'vitest';
import { EnergyVad, rmsOf } from './vad';

/** Feed `ms` worth of 50 ms frames at a constant level; return non-null events with times. */
function run(vad: EnergyVad, start: number, ms: number, level: number) {
  const events: [string, number][] = [];
  for (let t = start; t < start + ms; t += 50) {
    const ev = vad.feed(level, t);
    if (ev) events.push([ev, t]);
  }
  return { events, end: start + ms };
}

describe('EnergyVad', () => {
  it('detects speech after minSpeechMs and its end after silenceMs', () => {
    const vad = new EnergyVad();
    let r = run(vad, 0, 2000, 0.002); // quiet room
    expect(r.events).toEqual([]);
    r = run(vad, r.end, 1500, 0.08); // talking
    expect(r.events).toEqual([['speech-start', 2200]]);
    r = run(vad, r.end, 2500, 0.002); // stops
    expect(r.events).toEqual([['speech-end', 5250]]); // last loud frame 3450 + 1800
  });

  it('ignores blips shorter than minSpeechMs (door click, cough)', () => {
    const vad = new EnergyVad();
    run(vad, 0, 1000, 0.002);
    expect(run(vad, 1000, 100, 0.2).events).toEqual([]);
    expect(run(vad, 1100, 1000, 0.002).events).toEqual([]);
  });

  it('does not end on a short mid-sentence pause', () => {
    const vad = new EnergyVad();
    run(vad, 0, 500, 0.002);
    run(vad, 500, 1000, 0.08);
    expect(run(vad, 1500, 1500, 0.002).events).toEqual([]); // 1.5 s thinking pause < 1.8 s
    expect(run(vad, 3000, 1000, 0.08).events).toEqual([]);
    expect(vad.isSpeaking).toBe(true);
  });

  it('adapts to a noisy room: steady fan noise is not speech, louder voice is', () => {
    const vad = new EnergyVad();
    expect(run(vad, 0, 5000, 0.03).events).toEqual([]); // 0.03 > minRms, but it is the floor
    expect(vad.threshold).toBeGreaterThan(0.08);
    expect(run(vad, 5000, 600, 0.2).events.map((e) => e[0])).toEqual(['speech-start']);
  });

  it('reports a capped utterance as speech-timeout (noise — caller discards it)', () => {
    const vad = new EnergyVad({ maxUtteranceMs: 3000 });
    run(vad, 0, 500, 0.002);
    const r = run(vad, 500, 5000, 0.1);
    expect(r.events.map((e) => e[0])).toEqual(['speech-start', 'speech-timeout']);
    expect(r.events[1][1] - r.events[0][1]).toBeLessThanOrEqual(3000);
  });

  it('reset() clears the utterance but keeps the learned floor', () => {
    const vad = new EnergyVad();
    run(vad, 0, 5000, 0.03);
    const th = vad.threshold;
    run(vad, 5000, 600, 0.2);
    vad.reset();
    expect(vad.isSpeaking).toBe(false);
    expect(vad.threshold).toBeCloseTo(th, 6);
  });
});

describe('rmsOf', () => {
  it('computes root mean square', () => {
    expect(rmsOf(new Float32Array([0.5, -0.5, 0.5, -0.5]))).toBeCloseTo(0.5);
    expect(rmsOf(new Float32Array(0))).toBe(0);
  });
});

describe('EnergyVad calibration', () => {
  it('a call started in a noisy room calibrates instead of firing', () => {
    const vad = new EnergyVad();
    expect(run(vad, 0, 3000, 0.04).events).toEqual([]);
    expect(run(vad, 3000, 600, 0.25).events.map((e) => e[0])).toEqual(['speech-start']);
  });

  it('after a capped (noise) utterance, the same noise does not re-trigger', () => {
    const vad = new EnergyVad({ maxUtteranceMs: 3000 });
    run(vad, 0, 500, 0.002);
    run(vad, 500, 4000, 0.1); // TV switched on: start → capped end
    expect(run(vad, 4500, 5000, 0.1).events).toEqual([]);
  });
});

describe('EnergyVad onset', () => {
  it('isRising is true while a possible onset is being confirmed', () => {
    const vad = new EnergyVad();
    run(vad, 0, 1000, 0.002);
    expect(vad.isRising).toBe(false);
    vad.feed(0.1, 1000);
    expect(vad.isRising).toBe(true);
    vad.feed(0.002, 1050);
    expect(vad.isRising).toBe(false);
  });
});
