import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import RemindersCard from './RemindersCard';
import type { RemindersResponse } from '@/lib/types';

const mockUseReminders = vi.fn();

vi.mock('@/lib/hooks', () => ({
  useReminders: () => mockUseReminders(),
}));

vi.mock('@/lib/api', () => ({
  api: { completeReminder: vi.fn() },
}));

function hookState(data: Partial<RemindersResponse> | undefined) {
  return { data, error: undefined, isLoading: false, mutate: vi.fn() };
}

const PENDING = {
  id: 'p1',
  text: 'Call the dentist',
  time: '2026-07-06T15:00:00',
  status: 'pending',
  scheduled: true,
};

const DELIVERED = {
  id: 'd1',
  text: 'Take meds',
  time: '2026-07-05T09:00:00',
  status: 'completed',
  completed_at: '2026-07-05T09:00:12',
  acked_via: 'telegram',
  snooze_count: 0,
};

const FAILED = {
  id: 'f1',
  text: 'Water the plants',
  time: '2026-07-05T10:00:00',
  status: 'failed',
  completed_at: '2026-07-05T10:05:00',
  acked_via: null,
  snooze_count: 0,
};

const MISSED = {
  id: 'm1',
  text: 'Stretch break',
  time: '2026-07-05T11:00:00',
  status: 'missed',
  completed_at: '2026-07-05T11:30:00',
  acked_via: null,
  snooze_count: 0,
};

describe('RemindersCard trust layer', () => {
  beforeEach(() => {
    mockUseReminders.mockReset();
  });

  it('renders pending reminders without a delivery log when recent is empty', () => {
    mockUseReminders.mockReturnValue(
      hookState({ count: 1, scheduler_jobs: 1, reminders: [PENDING], recent: [] })
    );
    render(<RemindersCard />);
    expect(screen.getByText('Call the dentist')).toBeInTheDocument();
    expect(screen.queryByText(/last 24 h/i)).toBeNull();
    expect(screen.queryByText(/not delivered/i)).toBeNull();
  });

  it('shows the last-24h delivery log with per-state labels', () => {
    mockUseReminders.mockReturnValue(
      hookState({
        count: 0,
        scheduler_jobs: 0,
        reminders: [],
        recent: [DELIVERED, FAILED, MISSED],
      })
    );
    render(<RemindersCard />);
    expect(screen.getByText(/last 24 h/i)).toBeInTheDocument();
    expect(screen.getByText(/done via telegram/i)).toBeInTheDocument();
    expect(screen.getByText('Failed to deliver')).toBeInTheDocument();
    expect(screen.getByText('Missed')).toBeInTheDocument();
  });

  it('surfaces a not-delivered badge counting failed + missed', () => {
    mockUseReminders.mockReturnValue(
      hookState({
        count: 0,
        scheduler_jobs: 0,
        reminders: [],
        recent: [DELIVERED, FAILED, MISSED],
      })
    );
    render(<RemindersCard />);
    expect(screen.getByText('2 not delivered')).toBeInTheDocument();
  });

  it('lists problems before deliveries in the log', () => {
    mockUseReminders.mockReturnValue(
      hookState({
        count: 0,
        scheduler_jobs: 0,
        reminders: [],
        recent: [DELIVERED, MISSED, FAILED],
      })
    );
    render(<RemindersCard />);
    const texts = [FAILED.text, MISSED.text, DELIVERED.text];
    const rendered = screen
      .getAllByText(new RegExp(`^(${texts.join('|')})$`))
      .map((el) => el.textContent);
    expect(rendered).toEqual(['Water the plants', 'Stretch break', 'Take meds']);
  });

  it('tolerates a response without the recent field (older orchestrator)', () => {
    mockUseReminders.mockReturnValue(
      hookState({ count: 1, scheduler_jobs: 1, reminders: [PENDING] })
    );
    render(<RemindersCard />);
    expect(screen.getByText('Call the dentist')).toBeInTheDocument();
    expect(screen.queryByText(/last 24 h/i)).toBeNull();
  });
});
