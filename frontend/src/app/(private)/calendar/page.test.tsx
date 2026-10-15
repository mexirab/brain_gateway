import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import CalendarPage from './page';
import type { CalendarRangeResponse } from '@/lib/types';

const mockUseCalendarRange = vi.fn();

vi.mock('@/lib/hooks', () => ({
  useCalendarRange: (start: string, days: number) => mockUseCalendarRange(start, days),
}));

// Thursday 2026-10-15, noon local (TZ=America/New_York via the npm test script).
const NOW = new Date(2026, 9, 15, 12, 0, 0);
const THIS_MONDAY = '2026-10-12';
const NEXT_MONDAY = '2026-10-19';

function week(start: string, title: string): CalendarRangeResponse {
  return {
    ok: true,
    source: 'merged',
    count: 1,
    unavailable: [],
    web_url: null,
    start,
    days: 7,
    timezone: 'America/New_York',
    events: [
      {
        id: `ev-${start}`,
        title,
        start: `${start}T09:00:00-04:00`,
        end: `${start}T09:30:00-04:00`,
        location: null,
        description: null,
        all_day: false,
        source: 'caldav',
      },
    ],
  };
}

function hookState(data: CalendarRangeResponse | undefined, error?: Error, mutate = vi.fn()) {
  return { data, error, isLoading: !data && !error, mutate };
}

const loading = () => screen.queryByLabelText('Loading this week');

describe('CalendarPage', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    // jsdom has no scrollIntoView; the page calls it to bring today into view.
    Element.prototype.scrollIntoView = vi.fn();
    mockUseCalendarRange.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('asks for the current Monday-based week and renders its events', () => {
    mockUseCalendarRange.mockReturnValue(hookState(week(THIS_MONDAY, 'Team sync')));
    render(<CalendarPage />);
    expect(mockUseCalendarRange).toHaveBeenCalledWith(THIS_MONDAY, 7);
    expect(screen.getByText('Team sync')).toBeInTheDocument();
    expect(screen.getByText('Monday, Oct 12')).toBeInTheDocument();
    expect(loading()).toBeNull();
  });

  it('never shows another week’s events when data.start does not match', () => {
    mockUseCalendarRange.mockReturnValue(hookState(week('2026-10-05', 'Old meeting')));
    render(<CalendarPage />);
    expect(screen.queryByText('Old meeting')).toBeNull();
    expect(screen.queryByText('Monday, Oct 12')).toBeNull();
    expect(screen.queryAllByText('Nothing scheduled')).toHaveLength(0);
    expect(loading()).toBeInTheDocument();
  });

  it('drops last week’s events and shows the skeleton after paging forward', () => {
    // SWR still holding this week's answer while next week loads.
    mockUseCalendarRange.mockReturnValue(hookState(week(THIS_MONDAY, 'Team sync')));
    render(<CalendarPage />);
    expect(screen.getByText('Team sync')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Next week' }));

    expect(mockUseCalendarRange).toHaveBeenLastCalledWith(NEXT_MONDAY, 7);
    expect(screen.queryByText('Team sync')).toBeNull();
    expect(screen.queryByText('Monday, Oct 19')).toBeNull();
    expect(loading()).toBeInTheDocument();
  });

  it('shows the ErrorState with a working retry when the fetch fails', () => {
    const mutate = vi.fn();
    mockUseCalendarRange.mockReturnValue(hookState(undefined, new Error('API 500'), mutate));
    render(<CalendarPage />);
    expect(screen.getByText('Couldn’t load this week’s calendar.')).toBeInTheDocument();
    expect(screen.queryByText(/API 500/)).toBeNull();
    expect(loading()).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /try again/i }));
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it('prefers matching data over a lingering error', () => {
    mockUseCalendarRange.mockReturnValue(hookState(week(THIS_MONDAY, 'Team sync'), new Error('API 500')));
    render(<CalendarPage />);
    expect(screen.getByText('Team sync')).toBeInTheDocument();
    expect(screen.queryByText('Couldn’t load this week’s calendar.')).toBeNull();
  });
});
