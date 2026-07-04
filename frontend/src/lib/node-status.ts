import type { ClusterNode } from './constants';
import type { ServicesResponse } from './types';

/**
 * LIVE status of a cluster node, derived from real `/api/services` health
 * rather than the decorative flow animation.
 *
 * - `online`      — at least one of the node's services is healthy.
 * - `asleep`      — wake-on-demand node whose services are configured but not
 *                   currently healthy (e.g. Helios powered down between calls).
 * - `offline`     — always-on node whose services are configured but unhealthy,
 *                   or a known-unreachable box.
 * - `unknown`     — status not yet known (services data not loaded / unreachable).
 */
export type NodeStatus = 'online' | 'asleep' | 'offline' | 'unknown';

export interface NodeStatusInfo {
  status: NodeStatus;
  label: string;
  /** Tailwind text/border color token stem, e.g. 'success' | 'warning' | 'danger' | 'muted'. */
  tone: 'success' | 'warning' | 'danger' | 'muted';
}

const INFO: Record<NodeStatus, Omit<NodeStatusInfo, never>> = {
  online: { status: 'online', label: 'Online', tone: 'success' },
  asleep: { status: 'asleep', label: 'Asleep', tone: 'warning' },
  offline: { status: 'offline', label: 'Offline', tone: 'danger' },
  unknown: { status: 'unknown', label: 'Unknown', tone: 'muted' },
};

/**
 * Compute a node's live status from the services summary.
 *
 * `services` is undefined while the SWR fetch is in flight or failed — callers
 * get `unknown` in that case so the UI can show a neutral (non-green) badge
 * instead of faking liveness.
 */
export function nodeStatus(
  node: Pick<ClusterNode, 'power' | 'serviceKeys'>,
  services: ServicesResponse | undefined,
): NodeStatusInfo {
  // A box we know is unreachable is always offline, regardless of the probe.
  if (node.power === 'unreachable') return INFO.offline;

  // No live data yet → don't claim anything.
  if (!services) return INFO.unknown;

  const infos = node.serviceKeys
    .map((k) => services.services[k])
    .filter((s): s is NonNullable<typeof s> => Boolean(s));

  // None of this node's services are even configured — fall back to the
  // baseline power classification (orchestrator responded, so an always-on
  // node is up; a wake-on-demand node with nothing probe-able is unknown).
  if (infos.length === 0) {
    return node.power === 'always-on' ? INFO.online : INFO.unknown;
  }

  if (infos.some((s) => s.healthy)) return INFO.online;

  // Configured but nothing healthy: asleep for wake-on-demand, offline otherwise.
  return node.power === 'wake-on-demand' ? INFO.asleep : INFO.offline;
}
