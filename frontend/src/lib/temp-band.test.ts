import { describe, expect, it } from 'vitest';
import { barGeometry, tempBand } from './temp-band';

describe('tempBand', () => {
  it.each([
    [null, 'unknown'],
    [undefined, 'unknown'],
    [84.9, 'ok'],
    [90, 'warning'],
    [94.9, 'warning'],
    [95, 'critical'],
    [101, 'critical'],
  ] as const)('%s with 90/95 bands is %s', (temp, band) => {
    expect(tempBand(temp, 90, 95)).toBe(band);
  });

  it('treats a temperature at zero as a real reading', () => {
    expect(tempBand(0, 80, 85)).toBe('ok');
  });
});

describe('barGeometry', () => {
  it('puts the ticks where the thresholds really are (90/95 → 70–100° bar)', () => {
    const g = barGeometry(85, 90, 95);
    expect(g.min).toBe(70);
    expect(g.max).toBe(100);
    expect(g.progress).toBe(50);
    expect(g.warningPct).toBeCloseTo(66.67, 1);
    expect(g.criticalPct).toBeCloseTo(83.33, 1);
    // The fill at 85° stops short of the warning tick, matching the green band.
    expect(g.progress).toBeLessThan(g.warningPct);
  });

  it('clamps the fill and is empty without a reading', () => {
    expect(barGeometry(150, 90, 95).progress).toBe(100);
    expect(barGeometry(10, 90, 95).progress).toBe(0);
    expect(barGeometry(null, 90, 95).progress).toBe(0);
  });
});
