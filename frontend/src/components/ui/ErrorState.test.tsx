import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ErrorState } from './ErrorState';

describe('ErrorState', () => {
  it('renders friendly default copy with role="alert"', () => {
    render(<ErrorState />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/can't reach jess/i)).toBeInTheDocument();
  });

  it('only renders a Retry button when onRetry is provided', () => {
    const { rerender } = render(<ErrorState message="nope" />);
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull();
    rerender(<ErrorState message="nope" onRetry={() => {}} />);
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('calls onRetry when the Retry button is clicked', async () => {
    const onRetry = vi.fn();
    render(<ErrorState onRetry={onRetry} retryLabel="Reload" />);
    await userEvent.click(screen.getByRole('button', { name: 'Reload' }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('renders the compact variant with the given message', () => {
    render(<ErrorState compact message="Couldn’t load reminders." onRetry={() => {}} />);
    expect(screen.getByText('Couldn’t load reminders.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });
});
