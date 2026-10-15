// Date helpers for the /calendar week view. All in the browser's local time,
// which is the user's own timezone (the orchestrator returns ISO strings with
// offsets, so Date parses them to the right instant).

import type { CalendarEvent } from './types';

/** Local midnight of the Monday on or before `d`. */
export function mondayOf(d: Date): Date {
  const m = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const offset = (m.getDay() + 6) % 7; // Mon=0 … Sun=6
  m.setDate(m.getDate() - offset);
  return m;
}

export function addDays(d: Date, n: number): Date {
  const r = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  r.setDate(r.getDate() + n);
  return r;
}

/** YYYY-MM-DD of a local date (not toISOString, which is UTC). */
export function isoDate(d: Date): string {
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${mm}-${dd}`;
}

export function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/**
 * Events that touch the local day starting at `dayStart`, all-day first, then
 * by start time. Multi-day events appear on every day they overlap; a
 * zero-length event belongs to the day it starts in.
 */
export function eventsForDay(events: CalendarEvent[], dayStart: Date): CalendarEvent[] {
  const startMs = dayStart.getTime();
  const endMs = addDays(dayStart, 1).getTime();
  return events
    .filter((ev) => {
      const s = Date.parse(ev.start);
      const e = Date.parse(ev.end);
      if (Number.isNaN(s)) return false;
      if (s >= endMs) return false;
      if (!Number.isNaN(e) && e > s) return e > startMs;
      return s >= startMs;
    })
    .sort((a, b) => {
      if (a.all_day !== b.all_day) return a.all_day ? -1 : 1;
      return Date.parse(a.start) - Date.parse(b.start);
    });
}

const TIME_FMT = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' });
// Newer ICU puts a narrow no-break space before AM/PM; normalise to a plain space.
const fmtTime = (d: Date) => TIME_FMT.format(d).replace(/\u202f/g, ' ');

/** "9:00 AM – 9:30 AM", or "9:00 AM" for zero-length / unparseable ends. */
export function timeRange(ev: CalendarEvent): string {
  const s = new Date(ev.start);
  const e = new Date(ev.end);
  if (Number.isNaN(e.getTime()) || e.getTime() <= s.getTime()) return fmtTime(s);
  return `${fmtTime(s)} – ${fmtTime(e)}`;
}

/** True once a timed event has finished (for dimming past items today). */
export function isOver(ev: CalendarEvent, now: Date): boolean {
  if (ev.all_day) return false;
  const e = Date.parse(ev.end);
  const s = Date.parse(ev.start);
  return (Number.isNaN(e) || e <= s ? s : e) < now.getTime();
}
