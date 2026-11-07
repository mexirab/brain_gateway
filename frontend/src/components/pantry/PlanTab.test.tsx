import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import PlanTab from './PlanTab';
import { PantryApiError, type MealPlan } from '@/lib/pantry-api';

const planApi = vi.hoisted(() => ({
  current: vi.fn(),
  diet: vi.fn(),
  discard: vi.fn(),
  accept: vi.fn(),
}));

vi.mock('@/lib/pantry-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/pantry-api')>()),
  planApi,
}));
// Their own API calls aren't under test here.
vi.mock('./CookedOtherCard', () => ({ default: () => null }));
vi.mock('./DietSettingsCard', () => ({ default: () => null }));

function plan(overrides: Partial<MealPlan> = {}): MealPlan {
  return {
    id: 42,
    week_start: '2026-07-09',
    status: 'draft',
    profile_label: 'Balanced',
    notes: null,
    warnings: [],
    error: null,
    created_at: new Date().toISOString(),
    entries: [],
    recipes: {},
    batches: {},
    missing: [],
    ...overrides,
  };
}

/** The server's view: the draft stays until a discard call succeeds. Tests that
 *  make discard fail override `planApi.discard` after this, so it stays. */
function serverWith(draft: MealPlan, active: MealPlan | null = null) {
  const server: { active: MealPlan | null; draft: MealPlan | null } = { active, draft };
  planApi.current.mockImplementation(async () => ({ ...server }));
  planApi.discard.mockImplementation(async () => {
    server.draft = null;
    return { ok: true };
  });
  return server;
}

/** A generation older than STUCK_MS (10 min), so "Cancel it" shows. */
function stuckGeneration(): MealPlan {
  return plan({ status: 'generating', created_at: new Date(Date.now() - 11 * 60 * 1000).toISOString() });
}

describe('PlanTab', () => {
  beforeAll(() => {
    // jsdom has no scrollIntoView; the notice scrolls itself into view.
    Element.prototype.scrollIntoView = () => {};
  });
  beforeEach(() => {
    planApi.current.mockReset();
    planApi.discard.mockReset().mockResolvedValue({ ok: true });
    planApi.accept.mockReset();
    planApi.diet.mockReset().mockResolvedValue({ settings: {}, profiles: [] });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('Discard on a draft calls the API, the draft goes away and a notice says so', async () => {
    serverWith(plan());
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<PlanTab shown />);
    await userEvent.click(await screen.findByRole('button', { name: 'Discard' }));

    await waitFor(() => expect(planApi.discard).toHaveBeenCalledWith(42));
    expect(await screen.findByText('Draft thrown away.')).toBeInTheDocument();
    // The notice is set before run() reloads the plans; wait for the reload too.
    expect(await screen.findByRole('button', { name: 'Plan my week' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Discard' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('Discard next to an accepted plan says the accepted plan is unchanged', async () => {
    serverWith(plan({ week_start: '2026-07-16' }), plan({ id: 7, status: 'active' }));
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<PlanTab shown />);
    await userEvent.click(await screen.findByRole('button', { name: 'Discard' }));

    expect(await screen.findByText('Draft thrown away. Your accepted plan is unchanged.')).toBeInTheDocument();
    expect(planApi.discard).toHaveBeenCalledWith(42);
  });

  it('Discard does nothing when the confirm is cancelled', async () => {
    serverWith(plan());
    vi.spyOn(window, 'confirm').mockReturnValue(false);

    render(<PlanTab shown />);
    await userEvent.click(await screen.findByRole('button', { name: 'Discard' }));

    expect(planApi.discard).not.toHaveBeenCalled();
    expect(planApi.current).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Discard' })).toBeInTheDocument();
  });

  it('a 409 on discard shows the reason, then reloads to what the server has now', async () => {
    const server = serverWith(plan());
    // Discarded elsewhere (voice, another device) between page load and the tap.
    planApi.discard.mockImplementation(async () => {
      server.draft = null;
      throw new PantryApiError(409, 'plan is discarded');
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<PlanTab shown />);
    await userEvent.click(await screen.findByRole('button', { name: 'Discard' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('plan is discarded');
    expect(await screen.findByRole('button', { name: 'Plan my week' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('plan is discarded');
    expect(screen.queryByText(/Draft thrown away/)).not.toBeInTheDocument();
  });

  it('a network failure on discard keeps the draft and Discard usable again', async () => {
    serverWith(plan());
    planApi.discard.mockRejectedValue(new TypeError('Failed to fetch'));
    vi.spyOn(console, 'error').mockImplementation(() => {}); // friendlyError logs the raw error
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<PlanTab shown />);
    await userEvent.click(await screen.findByRole('button', { name: 'Discard' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/That didn.t work/);
    await waitFor(() => expect(planApi.current).toHaveBeenCalledTimes(2));
    // A busy flag left on would be the original symptom: a button that does nothing.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Discard' })).toBeEnabled());
    expect(screen.queryByText(/Draft thrown away/)).not.toBeInTheDocument();
  });

  it('"Cancel it" on a stuck generation calls the API and clears the card', async () => {
    serverWith(stuckGeneration());

    render(<PlanTab shown />);
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel it' }));

    await waitFor(() => expect(planApi.discard).toHaveBeenCalledWith(42));
    expect(await screen.findByText(/^Cancelled/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Jess is planning your week…')).not.toBeInTheDocument());
  });

  it('"Cancel it" sends one request when tapped twice', async () => {
    // No confirm dialog on this button, so only `busy` stops a second request
    // (which the server would answer with a 409 after the first succeeded).
    const server = serverWith(stuckGeneration());
    let finish: () => void = () => {};
    planApi.discard.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = () => {
            server.draft = null;
            resolve({ ok: true });
          };
        }),
    );

    render(<PlanTab shown />);
    const cancel = await screen.findByRole('button', { name: 'Cancel it' });
    await userEvent.click(cancel);
    await userEvent.click(cancel);

    expect(planApi.discard).toHaveBeenCalledTimes(1);
    expect(cancel).toBeDisabled();
    finish();
    expect(await screen.findByText(/^Cancelled/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('Accept hands the API result to its notice', async () => {
    serverWith(plan({ missing: ['rice'] }));
    planApi.accept.mockResolvedValue({ ok: true, added_to_list: 3, added_to_pantry: 0, reminders: 2 });

    render(<PlanTab shown />);
    await userEvent.click(await screen.findByRole('button', { name: /Accept plan/ }));

    expect(planApi.accept).toHaveBeenCalledWith(42, []);
    expect(
      await screen.findByText('Plan accepted: 3 new items on your grocery list, 2 cooking reminders set.'),
    ).toBeInTheDocument();
  });
});
