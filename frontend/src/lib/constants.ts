export const ORCHESTRATOR_URL =
  process.env.ORCHESTRATOR_URL || 'http://localhost:8888';

/**
 * Fallback name for the primary "brain" model, shown when the live `/health`
 * `primary` field isn't available (e.g. anonymous render or orchestrator down).
 * The diagrams prefer the live value; this is the single hard-coded default.
 */
export const PRIMARY_MODEL_FALLBACK = 'Primary LLM';

/**
 * Baseline power/reachability classification for a node.
 *
 * - `always-on`  — expected up whenever the cluster is up.
 * - `wake-on-demand` — power-tiered; asleep by default, woken on demand. A
 *   configured-but-unhealthy service on such a node means "asleep", not "down".
 * - `unreachable` — known-offline / test box; never expected to be live.
 */
export type NodePower = 'always-on' | 'wake-on-demand' | 'unreachable';

export interface ClusterNode {
  id: string;
  name: string;
  ip: string;
  gpu: string | null;
  role: string;
  services: readonly string[];
  power: NodePower;
  /**
   * Keys from the orchestrator `/api/services` summary that physically run on
   * this node. Used to derive LIVE status. Empty = no probe-able service maps
   * to this node (status is inferred from `power` + orchestrator reachability).
   */
  serviceKeys: readonly string[];
}

/**
 * Single source of truth for the cluster topology (2026-07-04).
 *
 * Both the NodeCard grid (architecture page) and the SVG SystemDiagram derive
 * node identity from this list, and node LIVE status from `serviceKeys` +
 * `/api/services`. Keep this list — not the diagrams — authoritative.
 */
export const CLUSTER_NODES: readonly ClusterNode[] = [
  {
    id: 'jupiter',
    name: 'Jupiter',
    ip: '10.0.0.248',
    gpu: null,
    role: 'Always-on gateway: orchestrator, frontend, monitoring & Home Assistant',
    services: [
      'Orchestrator :8888',
      'Frontend :3001',
      'Grafana :3000',
      'Prometheus / Loki / Alertmanager',
      'Home Assistant :8123',
    ],
    power: 'always-on',
    // Orchestrator + HA both live here. HA is probe-able via /api/services.
    serviceKeys: ['ha'],
  },
  {
    id: 'helios',
    name: 'Helios',
    ip: '10.0.0.195',
    gpu: 'RTX 5090',
    role: 'Primary LLM + TTS/STT — wake-on-demand (asleep by default)',
    services: [
      'Primary LLM (vLLM)',
      'TTS',
      'Whisper STT',
    ],
    power: 'wake-on-demand',
    serviceKeys: ['model', 'tts', 'stt'],
  },
  {
    id: 'saturn',
    name: 'Saturn',
    ip: '10.0.0.58',
    gpu: null,
    role: 'Vision + expert/reasoner models; off-box backup target',
    services: [
      'Qwen3VL-8B-Instruct :8010 (vision)',
      'Expert / reasoner :8084',
      'Backup target',
    ],
    power: 'always-on',
    serviceKeys: ['vision', 'expert'],
  },
  {
    id: 'uranus',
    name: 'Uranus',
    ip: '10.0.0.173',
    gpu: '2x RTX 5080',
    role: 'Test box — currently unreachable',
    services: ['Test / experimental'],
    power: 'unreachable',
    serviceKeys: [],
  },
] as const;

export const CAPABILITIES = [
  { icon: 'Mic', label: 'Voice Control', desc: '"Hey Jess" wake word' },
  { icon: 'Timer', label: 'Focus Timer', desc: 'Pomodoro + site blocking' },
  { icon: 'Calendar', label: 'Calendar', desc: 'Proactive scheduling' },
  { icon: 'Home', label: 'Smart Home', desc: 'Lights, climate, scenes' },
  { icon: 'Brain', label: 'Memory', desc: 'Personal knowledge base (RAG)' },
  { icon: 'Search', label: 'Web Search', desc: 'Real-time answers' },
] as const;
