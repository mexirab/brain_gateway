import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import AppearancePanel from './AppearancePanel';

function radio(name: string) {
  return screen.getByRole('radio', { name: new RegExp(name) });
}

describe('AppearancePanel', () => {
  beforeEach(() => {
    document.cookie = 'jess-theme=; Path=/; Max-Age=0';
    delete document.documentElement.dataset.theme;
  });

  it('renders a theme radiogroup with three options', () => {
    render(<AppearancePanel />);
    expect(screen.getByRole('radiogroup', { name: 'Theme' })).toBeInTheDocument();
    expect(screen.getAllByRole('radio')).toHaveLength(3);
  });

  it('checks the option matching <html data-theme> after mount', () => {
    document.documentElement.dataset.theme = 'system';
    render(<AppearancePanel />);
    expect(radio('System')).toHaveAttribute('aria-checked', 'true');
    expect(radio('Dark')).toHaveAttribute('aria-checked', 'false');
    expect(radio('Light')).toHaveAttribute('aria-checked', 'false');
  });

  it('treats a missing data-theme as dark', () => {
    render(<AppearancePanel />);
    expect(radio('Dark')).toHaveAttribute('aria-checked', 'true');
  });

  it('clicking Light applies it: cookie + data-theme + checked', () => {
    document.documentElement.dataset.theme = 'dark';
    render(<AppearancePanel />);
    fireEvent.click(radio('Light'));
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(document.cookie).toContain('jess-theme=light');
    expect(radio('Light')).toHaveAttribute('aria-checked', 'true');
    expect(radio('Dark')).toHaveAttribute('aria-checked', 'false');
  });

  it('ArrowRight moves the selection and focus to the next option, wrapping', () => {
    document.documentElement.dataset.theme = 'light';
    render(<AppearancePanel />);
    const group = screen.getByRole('radiogroup');
    fireEvent.keyDown(group, { key: 'ArrowRight' });
    expect(radio('System')).toHaveAttribute('aria-checked', 'true');
    expect(document.documentElement.dataset.theme).toBe('system');
    expect(radio('System')).toHaveFocus();
    fireEvent.keyDown(group, { key: 'ArrowRight' });
    expect(radio('Dark')).toHaveAttribute('aria-checked', 'true');
  });

  it('ArrowLeft wraps backwards; other keys do nothing', () => {
    document.documentElement.dataset.theme = 'dark';
    render(<AppearancePanel />);
    const group = screen.getByRole('radiogroup');
    fireEvent.keyDown(group, { key: 'Enter' });
    expect(radio('Dark')).toHaveAttribute('aria-checked', 'true');
    fireEvent.keyDown(group, { key: 'ArrowLeft' });
    expect(radio('System')).toHaveAttribute('aria-checked', 'true');
  });

  it('exactly one radio is tabbable, and it is the selected one', () => {
    document.documentElement.dataset.theme = 'light';
    render(<AppearancePanel />);
    const tabbable = screen.getAllByRole('radio').filter((r) => r.tabIndex === 0);
    expect(tabbable).toHaveLength(1);
    expect(tabbable[0]).toBe(radio('Light'));
  });
});
