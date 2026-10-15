import { describe, it, expect } from 'vitest';
import { addDays, eventsForDay, isOver, isoDate, mondayOf, sameDay, timeRange } from './calendar-week';
import type { CalendarEvent } from './types';

function ev(title: string, start: Date, end: Date, allDay = false): CalendarEvent {
  return {
    id: title,
    title,
    start: start.toISOString(),
    end: end.toISOString(),
    location: null,
    description: null,
    all_day: allDay,
  };
}

const at = (y: number, mo: number, d: number, h = 0, mi = 0) => new Date(y, mo - 1, d, h, mi);

describe('mondayOf', () => {
  it('returns local midnight of the same week Monday', () => {
    expect(isoDate(mondayOf(at(2026, 10, 15, 18)))).toBe('2026-10-12'); // Thu
    expect(isoDate(mondayOf(at(2026, 10, 12, 0, 1)))).toBe('2026-10-12'); // Mon
    expect(isoDate(mondayOf(at(2026, 10, 18, 23)))).toBe('2026-10-12'); // Sun
    expect(mondayOf(at(2026, 10, 15, 18)).getHours()).toBe(0);
  });
});

describe('addDays / isoDate / sameDay', () => {
  it('crosses month ends and DST without drifting off midnight', () => {
    expect(isoDate(addDays(at(2026, 10, 29), 7))).toBe('2026-11-05');
    expect(addDays(at(2026, 10, 29), 7).getHours()).toBe(0);
    expect(sameDay(at(2026, 10, 15, 1), at(2026, 10, 15, 23))).toBe(true);
    expect(sameDay(at(2026, 10, 15), at(2026, 10, 16))).toBe(false);
  });
});

describe('eventsForDay', () => {
  const day = at(2026, 10, 20);
  it('keeps overlapping events, all-day first, then by start', () => {
    const late = ev('late', at(2026, 10, 20, 15), at(2026, 10, 20, 16));
    const early = ev('early', at(2026, 10, 20, 9), at(2026, 10, 20, 10));
    const allDay = ev('trip', at(2026, 10, 19), at(2026, 10, 22), true);
    const tomorrow = ev('tomorrow', at(2026, 10, 21, 9), at(2026, 10, 21, 10));
    const overnight = ev('overnight', at(2026, 10, 19, 23), at(2026, 10, 20, 1));
    expect(eventsForDay([late, tomorrow, early, allDay, overnight], day).map((e) => e.title)).toEqual([
      'trip',
      'overnight',
      'early',
      'late',
    ]);
  });
  it('places zero-length events on their start day only', () => {
    const z = ev('reminder', at(2026, 10, 20, 8), at(2026, 10, 20, 8));
    expect(eventsForDay([z], day)).toHaveLength(1);
    expect(eventsForDay([z], at(2026, 10, 21))).toHaveLength(0);
  });
  it('drops events with unparseable starts', () => {
    expect(eventsForDay([{ ...ev('x', day, day), start: 'nope' }], day)).toHaveLength(0);
  });
});

describe('timeRange / isOver', () => {
  it('formats ranges and single times', () => {
    expect(timeRange(ev('a', at(2026, 10, 20, 9), at(2026, 10, 20, 9, 30)))).toBe('9:00 AM – 9:30 AM');
    expect(timeRange(ev('b', at(2026, 10, 20, 14), at(2026, 10, 20, 14)))).toBe('2:00 PM');
  });
  it('is over only after the end, never for all-day', () => {
    const e = ev('a', at(2026, 10, 20, 9), at(2026, 10, 20, 10));
    expect(isOver(e, at(2026, 10, 20, 9, 30))).toBe(false);
    expect(isOver(e, at(2026, 10, 20, 10, 1))).toBe(true);
    expect(isOver({ ...e, all_day: true }, at(2026, 10, 21))).toBe(false);
  });
});
