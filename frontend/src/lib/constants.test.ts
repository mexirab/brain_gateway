import { describe, it, expect } from 'vitest';
import { CLUSTER_NODES } from './constants';

describe('CLUSTER_NODES topology (2026-11-26 ground truth)', () => {
  const byId = Object.fromEntries(CLUSTER_NODES.map((n) => [n.id, n]));

  it('has exactly the 3 real nodes', () => {
    expect(CLUSTER_NODES.map((n) => n.id).sort()).toEqual(['helios', 'jupiter', 'saturn'].sort());
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

  it('puts the live TTS on Jupiter (one of its two RTX 5080s)', () => {
    expect(byId.jupiter.gpu).toBe('2x RTX 5080');
    expect(byId.jupiter.serviceKeys).toEqual(['ha', 'tts']);
    expect(byId.jupiter.services.join(' ')).toMatch(/TTS/);
  });

  it('does NOT contain the dead Pi (10.0.0.106) or a standalone HA node', () => {
    for (const node of CLUSTER_NODES) expect(node.ip).not.toBe('10.0.0.106');
    expect(byId.ha).toBeUndefined();
  });

  it('recasts Helios as wake-on-demand primary LLM + STT (no orchestrator, no TTS)', () => {
    expect(byId.helios.ip).toBe('10.0.0.195');
    expect(byId.helios.power).toBe('wake-on-demand');
    const svc = byId.helios.services.join(' ');
    expect(svc).not.toMatch(/Orchestrator/i);
    expect(svc).not.toMatch(/Open WebUI/i);
    expect(svc).not.toMatch(/Frontend/i);
    expect(byId.helios.serviceKeys).toContain('model');
    expect(byId.helios.serviceKeys).not.toContain('tts');
  });

  it('does NOT contain the decommissioned Uranus test box (10.0.0.173)', () => {
    expect(byId.uranus).toBeUndefined();
    for (const node of CLUSTER_NODES) expect(node.ip).not.toBe('10.0.0.173');
  });

  it('reflects the current model layer (llama.cpp brain, Parakeet STT)', () => {
    const svc = byId.helios.services.join(' ');
    expect(svc).toMatch(/llama\.cpp/);
    expect(svc).toMatch(/Parakeet/);
    expect(svc).not.toMatch(/vLLM|Whisper/);
  });

  it('keeps Saturn as a backup box only (no TTS, vision or expert probes)', () => {
    expect(byId.saturn.serviceKeys).toHaveLength(0);
    const svc = byId.saturn.services.join(' ');
    expect(svc).toMatch(/backup/i);
    expect(svc).not.toMatch(/TTS|Expert/i);
  });

  it('does not hard-code a stale model name or RAG doc count', () => {
    const blob = JSON.stringify(CLUSTER_NODES);
    expect(blob).not.toMatch(/Qwen3\.5-27B/);
    expect(blob).not.toMatch(/154/);
  });
});
