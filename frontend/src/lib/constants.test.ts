import { describe, it, expect } from 'vitest';
import { CLUSTER_NODES } from './constants';

describe('CLUSTER_NODES topology (2026-07-04 ground truth)', () => {
  const byId = Object.fromEntries(CLUSTER_NODES.map((n) => [n.id, n]));

  it('has exactly the 4 real nodes', () => {
    expect(CLUSTER_NODES.map((n) => n.id).sort()).toEqual(
      ['helios', 'jupiter', 'saturn', 'uranus'].sort(),
    );
  });

  it('includes Jupiter as the always-on gateway on 10.0.0.248', () => {
    expect(byId.jupiter).toBeDefined();
    expect(byId.jupiter.ip).toBe('10.0.0.248');
    expect(byId.jupiter.power).toBe('always-on');
    // Owns orchestrator + HA (migrated off the dead Pi).
    const svc = byId.jupiter.services.join(' ');
    expect(svc).toMatch(/Orchestrator/i);
    expect(svc).toMatch(/Home Assistant/i);
  });

  it('does NOT contain the dead Pi (10.0.0.106) or a standalone HA node', () => {
    for (const node of CLUSTER_NODES) expect(node.ip).not.toBe('10.0.0.106');
    expect(byId.ha).toBeUndefined();
  });

  it('recasts Helios as wake-on-demand primary LLM + TTS/STT (no orchestrator)', () => {
    expect(byId.helios.ip).toBe('10.0.0.195');
    expect(byId.helios.power).toBe('wake-on-demand');
    const svc = byId.helios.services.join(' ');
    expect(svc).not.toMatch(/Orchestrator/i);
    expect(svc).not.toMatch(/Open WebUI/i);
    expect(svc).not.toMatch(/Frontend/i);
    expect(byId.helios.serviceKeys).toContain('model');
  });

  it('flags Uranus as an unreachable test box', () => {
    expect(byId.uranus.power).toBe('unreachable');
    expect(byId.uranus.serviceKeys).toHaveLength(0);
  });

  it('does not hard-code a stale model name or RAG doc count', () => {
    const blob = JSON.stringify(CLUSTER_NODES);
    expect(blob).not.toMatch(/Qwen3\.5-27B/);
    expect(blob).not.toMatch(/154/);
  });
});
