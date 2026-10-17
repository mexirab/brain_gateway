import { describe, it, expect, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import LightTile from './LightTile';
import { HomeControlsContext, type HomeControls } from './HomeContext';
import type { HAEntity } from '@/lib/types';

const light: HAEntity = {
  entity_id: 'light.desk',
  friendly_name: 'Desk',
  state: 'on',
  supports_brightness: true,
  brightness: 128,
};

function renderTile(entity: HAEntity, overrides: Partial<HomeControls> = {}) {
  const run = vi.fn().mockResolvedValue(true);
  const ctx: HomeControls = { run, errors: {}, pending: {}, ...overrides };
  render(
    <HomeControlsContext.Provider value={ctx}>
      <LightTile entity={entity} />
    </HomeControlsContext.Provider>,
  );
  return run;
}

describe('LightTile', () => {
  it('tapping an on light turns it off', () => {
    const run = renderTile(light);
    fireEvent.click(screen.getByRole('button', { pressed: true }));
    expect(run).toHaveBeenCalledWith(light, 'turn_off', undefined, { state: 'off' });
  });

  it('shows brightness and debounces slider changes into one call', () => {
    vi.useFakeTimers();
    const run = renderTile(light);
    expect(screen.getByText('On · 50%')).toBeInTheDocument();
    const slider = screen.getByRole('slider', { name: 'Desk brightness' });
    fireEvent.change(slider, { target: { value: '30' } });
    fireEvent.change(slider, { target: { value: '70' } });
    expect(run).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(400);
    });
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith(light, 'turn_on', { brightness_pct: 70 }, { state: 'on', brightness: 179 });
    vi.useRealTimers();
  });

  it('hides the slider when off or not dimmable', () => {
    renderTile({ ...light, state: 'off' });
    expect(screen.queryByRole('slider')).toBeNull();
  });

  it('shows the error in place of the state', () => {
    renderTile(light, { errors: { 'light.desk': 'Not allowed from the dashboard' } });
    expect(screen.getByText('Not allowed from the dashboard')).toBeInTheDocument();
  });
});
