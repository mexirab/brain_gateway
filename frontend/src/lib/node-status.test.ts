import { describe, it, expect } from 'vitest';
import { nodeStatus } from './node-status';
import { CLUSTER_NODES } from './constants';
import type { ServicesResponse, ServiceInfo } from './types';

function svc(overrides: Partial<ServiceInfo>): ServiceInfo {
  return {
    name: 'x',
    configured: true,
    healthy: false,
    last_check_ago: '5s',
    last_error: '',
    features_disabled_when_down: [],
    ...overrides,
  };
}

function services(entries: Record<string, Partial<ServiceInfo>>): ServicesResponse {
  const map: Record<string, ServiceInfo> = {};
  for (const [k, v] of Object.entries(entries)) map[k] = svc(v);
  return { services: map, unconfigured: [] };
}

const HELIOS = CLUSTER_NODES.find((n) => n.id === 'helios')!;
const JUPITER = CLUSTER_NODES.find((n) => n.id === 'jupiter')!;
const SATURN = CLUSTER_NODES.find((n) => n.id === 'saturn')!;
const URANUS = CLUSTER_NODES.find((n) => n.id === 'uranus')!;

describe('nodeStatus', () => {
  it('returns unknown when services data has not loaded', () => {
    expect(nodeStatus(HELIOS, undefined).status).toBe('unknown');
  });

  it('marks a known-unreachable box (Uranus) offline regardless of probe', () => {
    expect(nodeStatus(URANUS, undefined).status).toBe('offline');
    expect(nodeStatus(URANUS, services({})).status).toBe('offline');
  });

  it('marks Helios ASLEEP when its model is configured but not healthy', () => {
    const info = nodeStatus(HELIOS, services({ model: { healthy: false } }));
    expect(info.status).toBe('asleep');
    expect(info.tone).toBe('warning');
  });

  it('marks Helios ONLINE when any of its services is healthy', () => {
    const info = nodeStatus(
      HELIOS,
      services({ model: { healthy: false }, tts: { healthy: true } }),
    );
    expect(info.status).toBe('online');
  });

  it('marks an always-on node (Saturn) OFFLINE when its services are down', () => {
    const info = nodeStatus(
      SATURN,
      services({ vision: { healthy: false }, expert: { healthy: false } }),
    );
    expect(info.status).toBe('offline');
    expect(info.tone).toBe('danger');
  });

  it('marks Saturn ONLINE when vision is healthy', () => {
    expect(
      nodeStatus(SATURN, services({ vision: { healthy: true } })).status,
    ).toBe('online');
  });

  it('reflects HA health for Jupiter (HA runs on Jupiter)', () => {
    expect(nodeStatus(JUPITER, services({ ha: { healthy: true } })).status).toBe('online');
    expect(nodeStatus(JUPITER, services({ ha: { healthy: false } })).status).toBe('offline');
  });

  it('falls back to always-on = online when a node has no probe-able service', () => {
    // Jupiter with no ha entry in the summary → orchestrator responded, so up.
    expect(nodeStatus(JUPITER, services({})).status).toBe('online');
  });
});
