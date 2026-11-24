import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// The display-mode branch is the only thing under test: everything the layout fetches or mounts
// besides the two menus is stubbed out.
const state = vi.hoisted(() => ({ kiosk: false, authenticated: true }));
const redirect = vi.hoisted(() => vi.fn());

vi.mock('@/lib/display-mode.server', () => ({ isKioskDisplayRequest: () => state.kiosk }));
vi.mock('@/lib/auth', () => ({ isAuthenticated: async () => state.authenticated }));
vi.mock('@/lib/features.server', () => ({ getFeatureFlags: async () => ({}) }));
vi.mock('@/lib/branding.server', () => ({ getAssistantName: async () => 'Jess' }));
vi.mock('next/navigation', () => ({ redirect }));
vi.mock('@/components/layout/Sidebar', () => ({ default: () => <nav data-testid="sidebar" /> }));
vi.mock('@/components/layout/MobileNav', () => ({ default: () => <nav data-testid="mobile-nav" /> }));
vi.mock('@/components/providers/SessionExpiredWatcher', () => ({ default: () => null }));
vi.mock('@/components/providers/ServiceWorkerRegistrar', () => ({ default: () => null }));
vi.mock('@/components/providers/SWRProvider', () => ({
  SWRProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

import PrivateLayout from './layout';

async function renderLayout() {
  render(await PrivateLayout({ children: <p>page body</p> }));
}

describe('PrivateLayout display mode', () => {
  beforeEach(() => {
    state.kiosk = false;
    state.authenticated = true;
    redirect.mockReset();
  });

  it('shows both menus normally', async () => {
    await renderLayout();
    expect(screen.getByTestId('sidebar')).toBeInTheDocument();
    expect(screen.getByTestId('mobile-nav')).toBeInTheDocument();
    expect(screen.getByText('page body')).toBeInTheDocument();
  });

  it('hides both menus on a display-only screen but keeps the page', async () => {
    state.kiosk = true;
    await renderLayout();
    expect(screen.queryByTestId('sidebar')).not.toBeInTheDocument();
    expect(screen.queryByTestId('mobile-nav')).not.toBeInTheDocument();
    expect(screen.getByText('page body')).toBeInTheDocument();
    expect(screen.getByRole('main')).toBeInTheDocument();
  });

  it('still sends a visitor without a session to /login, kiosk or not', async () => {
    state.kiosk = true;
    state.authenticated = false;
    await renderLayout();
    expect(redirect).toHaveBeenCalledWith('/login');
  });
});
