import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import SystemDiagram from './SystemDiagram';

vi.mock('@/lib/hooks', () => ({
  useHealth: () => ({ data: undefined }),
  useServices: () => ({ data: undefined }),
}));

describe('SystemDiagram (current deployment)', () => {
  it('shows the live model layer, not the retired one', () => {
    const { container } = render(<SystemDiagram />);
    const text = container.textContent ?? '';
    expect(screen.getByText('Parakeet STT')).toBeTruthy();
    expect(screen.getByText('Qwen3.8 TURBO')).toBeTruthy();
    expect(text).toMatch(/llama\.cpp/);
    expect(text).not.toMatch(/Whisper|YNAB|RAG Docs|Telegram/);
  });

  it('marks the ATOM Echo voice path as disabled', () => {
    render(<SystemDiagram />);
    expect(screen.getByText('Voice · disabled')).toBeTruthy();
  });

  it('lists the real background jobs (5-min calendar poll, no email poll)', () => {
    const { container } = render(<SystemDiagram />);
    const text = container.textContent ?? '';
    expect(text).toMatch(/Calendar Poll/);
    expect(text).toMatch(/Every 5 min/);
    expect(text).not.toMatch(/Email Poll/);
    for (const job of ['Evening Brief', 'Wind-down', 'Self-care Nudges', 'Reminders']) {
      expect(text).toContain(job);
    }
  });

  it('includes the infrastructure nodes', () => {
    const { container } = render(<SystemDiagram />);
    const text = container.textContent ?? '';
    for (const label of [
      'Phone Push', 'Pushover · ntfy', 'Vision', 'Home Assistant',
      'Actual Budget', 'Radicale CalDAV', 'Monitoring', 'Nightly Backups',
    ]) {
      expect(text).toContain(label);
    }
  });
});
