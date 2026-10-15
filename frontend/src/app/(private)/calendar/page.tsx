'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CalendarDays, ChevronLeft, ChevronRight, ExternalLink, MapPin } from 'lucide-react';
import { Badge, Button, Card, EmptyState, ErrorState, PageHeader, Skeleton } from '@/components/ui';
import { useCalendarRange } from '@/lib/hooks';
import { addDays, eventsForDay, isOver, isoDate, mondayOf, sameDay, timeRange } from '@/lib/calendar-week';

const DAY_HEADING = new Intl.DateTimeFormat('en-US', { weekday: 'long', month: 'short', day: 'numeric' });
const SHORT = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });
const SHORT_YEAR = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
const TAP = 'min-h-[44px] min-w-[44px]';

function weekLabel(start: Date, now: Date): string {
  const end = addDays(start, 6);
  // Show the year when the week isn't (entirely) in the current year.
  const fmt = start.getFullYear() === now.getFullYear() && end.getFullYear() === now.getFullYear() ? SHORT : SHORT_YEAR;
  return `${fmt.format(start)} – ${fmt.format(end)}`;
}

export default function CalendarPage() {
  // Week shown = the Monday it starts on. `now` ticks each minute so "today"
  // highlighting and finished-event dimming stay right on an open tab.
  const [now, setNow] = useState(() => new Date());
  const [weekStart, setWeekStart] = useState(() => mondayOf(new Date()));
  const headingRef = useRef<HTMLHeadingElement>(null);
  const todayRef = useRef<HTMLDivElement>(null);
  const scrolledRef = useRef(false);
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(t);
  }, []);

  const startKey = isoDate(weekStart);
  const { data, error, mutate } = useCalendarRange(startKey, 7);
  // Only ever render a response for the week on screen — never last week's
  // events under this week's headings (that would read as an empty, free week).
  const fresh = data?.start === startKey ? data : undefined;
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)), [weekStart]);
  const events = fresh?.events ?? [];
  const unavailable = fresh?.unavailable ?? [];
  const nothingAnswered = fresh?.source === 'none';
  const webUrl = data?.web_url && /^https?:\/\//.test(data.web_url) ? data.web_url : null;
  const isThisWeek = sameDay(weekStart, mondayOf(now));
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const tzMismatch = fresh?.timezone && fresh.timezone !== browserTz ? fresh.timezone : null;

  // On first load of the current week, bring today into view (on a phone,
  // Saturday is the 6th card down).
  useEffect(() => {
    if (fresh && isThisWeek && !scrolledRef.current && todayRef.current) {
      scrolledRef.current = true;
      todayRef.current.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }, [fresh, isThisWeek]);

  const goTo = (w: Date) => {
    setWeekStart(w);
    headingRef.current?.focus();
  };

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        title="Calendar"
        icon={<CalendarDays size={22} />}
        description="Your personal calendar and work meetings, as Jess sees them."
        actions={
          webUrl ? (
            <a
              href={webUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="min-h-[44px] px-2 rounded flex items-center gap-1 text-sm text-content-muted hover:text-brand transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
            >
              Manage calendars
              <span className="sr-only"> (opens in new tab)</span>
              <ExternalLink size={14} aria-hidden />
            </a>
          ) : null
        }
      />

      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            icon
            className={TAP}
            aria-label="Previous week"
            onClick={() => goTo(addDays(weekStart, -7))}
          >
            <ChevronLeft size={18} aria-hidden />
          </Button>
          <Button
            variant="ghost"
            icon
            className={TAP}
            aria-label="Next week"
            onClick={() => goTo(addDays(weekStart, 7))}
          >
            <ChevronRight size={18} aria-hidden />
          </Button>
          <h2
            ref={headingRef}
            tabIndex={-1}
            className="ml-1 text-base font-semibold text-content-primary focus:outline-none"
            aria-live="polite"
          >
            {weekLabel(weekStart, now)}
          </h2>
        </div>
        <Button
          variant="secondary"
          size="sm"
          className="min-h-[44px]"
          aria-disabled={isThisWeek}
          disabled={isThisWeek}
          onClick={() => goTo(mondayOf(new Date()))}
        >
          This week
        </Button>
      </div>

      {fresh && unavailable.length > 0 && (
        <p role="status" className="mb-3 flex items-start gap-1.5 text-sm text-warning">
          <AlertTriangle size={14} aria-hidden className="mt-0.5 shrink-0" />
          <span>
            {`Couldn’t reach your ${unavailable.join(' or ')} calendar${unavailable.length > 1 ? 's' : ''}`}
            {nothingAnswered ? ' — this week’s events are unknown.' : ' — this may be incomplete.'}
          </span>
        </p>
      )}

      {tzMismatch && (
        <p className="mb-3 text-xs text-content-muted">
          Times are shown in this device’s time zone ({browserTz}); Jess uses {tzMismatch}.
        </p>
      )}

      {!fresh && error && (
        <Card>
          <ErrorState message="Couldn’t load this week’s calendar." onRetry={() => mutate()} />
        </Card>
      )}

      {!fresh && !error && (
        <div className="space-y-3" aria-busy="true" aria-label="Loading this week">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-20" />
          ))}
        </div>
      )}

      {fresh && nothingAnswered && unavailable.length === 0 && (
        <Card>
          <EmptyState
            icon={<CalendarDays size={32} />}
            title="No calendar connected"
            description="Connect your calendar in Jess’s CalDAV settings (see docs/CALENDAR.md)."
          />
        </Card>
      )}

      {fresh && nothingAnswered && unavailable.length > 0 && (
        <Card>
          <ErrorState message="None of your calendars answered." onRetry={() => mutate()} />
        </Card>
      )}

      {fresh && !nothingAnswered && (
        <div className="space-y-3">
          {days.map((day) => {
            const dayEvents = eventsForDay(events, day);
            const today = sameDay(day, now);
            return (
              <div key={isoDate(day)} ref={today ? todayRef : undefined} className="scroll-mt-4">
                <Card className={today ? 'border-brand/50' : ''}>
                  <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold text-content-primary">
                    {DAY_HEADING.format(day)}
                    {today && <Badge tone="brand">Today</Badge>}
                  </h3>
                  {dayEvents.length === 0 ? (
                    <p className="text-sm text-content-muted">Nothing scheduled</p>
                  ) : (
                    <ul className="space-y-2">
                      {dayEvents.map((ev) => {
                        const done = today && isOver(ev, now);
                        return (
                          <li
                            key={`${ev.id}|${ev.start}`}
                            className={`flex flex-col gap-1 rounded-lg border border-line/30 bg-surface-raised/40 p-2.5 sm:flex-row sm:gap-3 ${done ? 'opacity-60' : ''}`}
                          >
                            <span className="shrink-0 text-xs text-content-muted sm:w-32 sm:pt-0.5">
                              {ev.all_day ? 'All day' : timeRange(ev)}
                              {done && <span className="sr-only"> (finished)</span>}
                            </span>
                            <div className="min-w-0 flex-1">
                              <p className="text-sm font-medium text-white [overflow-wrap:anywhere]">
                                {ev.title}
                                {ev.source === 'exchange' && (
                                  <Badge tone="info" className="ml-2 align-middle">
                                    Work
                                  </Badge>
                                )}
                              </p>
                              {ev.location && (
                                <p className="mt-0.5 flex items-start gap-1 text-xs text-content-muted">
                                  <MapPin size={12} aria-hidden className="mt-0.5 shrink-0" />
                                  <span className="min-w-0 [overflow-wrap:anywhere]">{ev.location}</span>
                                </p>
                              )}
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </Card>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
