import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import SystemDiagram, { serviceDotColor } from './SystemDiagram';
import type { ServicesResponse, ServiceInfo } from '@/lib/types';

vi.mock('@/lib/hooks', () => ({
  useHealth: () => ({ data: undefined }),
  useServices: () => ({ data: undefined }),
}));

describe('SystemDiagram (current deployment)', () => {
  it('shows the live model layer, not the retired one', () => {
    const { container } = render(<SystemDiagram />);
    const text = container.textContent ?? '';
    expect(screen.getByText('Parakeet STT')).toBeTruthy();
    expect(screen.getByText('Qwen3.8-Flash-Next')).toBeTruthy();
    expect(text).not.toMatch(/TURBO/);
    expect(text).toMatch(/llama\.cpp/);
    expect(text).not.toMatch(/Whisper|YNAB|RAG Docs|Telegram/);
  });

  it('colours a down Saturn TTS red, a sleeping Helios brain amber', () => {
    const info = (healthy: boolean): ServiceInfo => ({
      name: 'x', configured: true, healthy, last_check_ago: '5s', last_error: '',
      features_disabled_when_down: [],
    });
    const services = (entries: Record<string, boolean>): ServicesResponse => ({
      services: Object.fromEntries(Object.entries(entries).map(([k, v]) => [k, info(v)])),
      unconfigured: [],
    });
    const node = (service: string) => ({ service }) as Parameters<typeof serviceDotColor>[0];
    expect(serviceDotColor(node('tts'), services({ tts: false }))).toBe('#fb7185');
    expect(serviceDotColor(node('model'), services({ model: false }))).toBe('#fbbf24');
    expect(serviceDotColor(node('tts'), services({ tts: true }))).toBe('#34d399');
    expect(serviceDotColor(node('tts'), undefined)).toBeNull();
  });

  it('places the TTS on Saturn', () => {
    const { container } = render(<SystemDiagram />);
    expect(container.textContent ?? '').toContain('Voice clone · Saturn');
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
