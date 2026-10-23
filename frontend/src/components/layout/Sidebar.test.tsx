import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import Sidebar from './Sidebar';
import { ALL_FEATURES_ENABLED } from '@/lib/features';

const pathname = vi.hoisted(() => ({ value: '/dashboard' as string | null }));

vi.mock('next/navigation', () => ({
  usePathname: () => pathname.value,
}));

// The badge polls the inbox over SWR; irrelevant here.
vi.mock('./ChatNavBadge', () => ({ default: () => null }));

function navLinks() {
  return within(screen.getByRole('navigation', { name: 'Primary' })).getAllByRole('link');
}

describe('Sidebar', () => {
  beforeEach(() => {
    pathname.value = '/dashboard';
  });

  it('marks only the current page with aria-current', () => {
    pathname.value = '/finance/boss-battle';
    render(<Sidebar flags={ALL_FEATURES_ENABLED} appName="Jess" />);
    const current = navLinks().filter((a) => a.getAttribute('aria-current') === 'page');
    expect(current).toHaveLength(1);
    expect(current[0]).toHaveAttribute('href', '/finance');
  });

  it('marks nothing when the pathname is null', () => {
    pathname.value = null;
    render(<Sidebar flags={ALL_FEATURES_ENABLED} appName="Jess" />);
    expect(navLinks().some((a) => a.hasAttribute('aria-current'))).toBe(false);
  });

  it('shows every item when all features are enabled', () => {
    render(<Sidebar flags={ALL_FEATURES_ENABLED} appName="Jess" />);
    const hrefs = navLinks().map((a) => a.getAttribute('href'));
    expect(hrefs).toEqual(expect.arrayContaining(['/workouts', '/meals', '/pantry', '/finance']));
  });

  it('hides feature-gated items whose flag is off', () => {
    render(
      <Sidebar
        flags={{ workouts_enabled: false, meals_enabled: true, pantry_enabled: false, jess_advanced: false }}
        appName="Jess"
      />,
    );
    const hrefs = navLinks().map((a) => a.getAttribute('href'));
    expect(hrefs).not.toContain('/workouts');
    expect(hrefs).not.toContain('/pantry');
    expect(hrefs).not.toContain('/finance');
    expect(hrefs).toContain('/meals');
    expect(hrefs).toContain('/dashboard');
    expect(hrefs).toContain('/settings');
  });

  it('shows the app name and its uppercase initial', () => {
    render(<Sidebar flags={ALL_FEATURES_ENABLED} appName="nova" />);
    const home = screen.getAllByRole('link').find((a) => a.getAttribute('href') === '/')!;
    expect(home).toHaveTextContent('nova');
    expect(within(home).getByText('N')).toBeInTheDocument();
  });
});
