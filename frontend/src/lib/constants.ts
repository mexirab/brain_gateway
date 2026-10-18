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
   * this node. Used to derive LIVE status. Empty = nothing the orchestrator
   * probes runs here, so status is `unknown` (or `offline` if unreachable).
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
    role: 'Always-on hub: orchestrator, dashboard, Home Assistant, budget, calendar & monitoring',
    services: [
      'Orchestrator :8888',
      'Dashboard (HTTPS via Tailscale)',
      'Home Assistant :8123',
      'Actual Budget',
      'Radicale CalDAV',
      'Grafana / Prometheus / Loki / Alertmanager',
    ],
    power: 'always-on',
    // Orchestrator + HA both live here. HA is probe-able via /api/services.
    serviceKeys: ['ha'],
  },
  {
    id: 'helios',
    name: 'Helios',
    ip: '10.0.0.195',
    gpu: 'RTX 5090 + RTX PRO 5000',
    role: 'GPU model layer — asleep most of the time, woken by an HA smart plug',
    services: [
      'Brain: Qwen3.8 TURBO on llama.cpp (RTX 5090) — also does vision',
      'TTS: voice clone (RTX PRO 5000)',
      'STT: Parakeet TDT v2, int8 ONNX on CPU',
    ],
    power: 'wake-on-demand',
    serviceKeys: ['model', 'vision', 'tts', 'stt'],
  },
  {
    id: 'saturn',
    name: 'Saturn',
    ip: '10.0.0.58',
    gpu: 'RTX 3080 + RTX 3090',
    role: 'Off-box nightly backup target (no runtime services)',
    services: [
      'Nightly backups: Jess state, HA config, calendars, budget',
      'Expert model :8084 — deprecated',
    ],
    power: 'always-on',
    // Nothing the orchestrator probes runs here any more (vision moved to the
    // Helios brain), so status is "unknown" rather than a guessed "online".
    serviceKeys: [],
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
  { icon: 'Mic', label: 'Voice Chat', desc: 'Dashboard mic + spoken replies' },
  { icon: 'Timer', label: 'Focus Timer', desc: 'Pomodoro + body doubling' },
  { icon: 'Calendar', label: 'Calendar', desc: 'Proactive scheduling' },
  { icon: 'Home', label: 'Smart Home', desc: 'Lights, climate, scenes' },
  { icon: 'Brain', label: 'Memory', desc: 'MemPalace — learns as you chat' },
  { icon: 'Search', label: 'Web Search', desc: 'Real-time answers' },
] as const;
