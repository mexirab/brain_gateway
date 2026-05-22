// First-boot setup wizard API client — `/api/setup/*`.
// Routes through /api/proxy/[...path] for bearer auth (the proxy injects it).

const PROXY = '/api/proxy';

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${PROXY}${path}`);
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`Setup API ${res.status}: ${detail || res.statusText}`);
  }
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${PROXY}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new Error(`Setup API ${res.status}: ${detail || res.statusText}`);
  }
  return res.json();
}

// ----- Types ----- (shapes mirror orchestrator/routes_setup.py)

export interface SetupStatus {
  ok: boolean;
  setup_completed: boolean;
  completed_at: string | null;
}

/** Shape of the `recommendation` block inside `HardwareScan.scan`.
 *  Produced by `scripts/detect_hardware.sh --json`. */
export interface HardwareRecommendation {
  model: string | null;
  quantization: string;
  max_model_len: number;
  gpu_mem_util: number;
  vision_capable: boolean;
}

/** Shape of `HardwareScan.scan`. Mirrors `scripts/detect_hardware.sh --json`. */
export interface HardwareScanData {
  gpus: { index: number; name: string; vram_gib: number }[];
  gpu_count: number;
  driver: string;
  ram_gib: number;
  largest_gpu_gib: number;
  vram_tier: number | null;
  tensor_parallel_advisory: unknown;
  recommendation: HardwareRecommendation;
}

export interface HardwareScan {
  ok: boolean;
  available: boolean;
  /** Present only when available — the cached detect_hardware.sh --json scan. */
  scan?: HardwareScanData;
  /** Present only when unavailable — operator instruction to produce the scan. */
  hint?: string;
}

export interface SetupCompleteResult {
  ok: boolean;
  setup_completed: boolean;
  completed_at: string | null;
}

/** Per-key state from `GET /api/setup/env`. Secrets never include `value`. */
export interface EnvKeyStatus {
  secret: boolean;
  service: string;
  description: string;
  present: boolean;
  value?: string;
}

export interface EnvStatus {
  ok: boolean;
  /** True once `POST /api/setup/complete` has fired — write/delete return 410. */
  locked: boolean;
  /** Any write since process start means the orchestrator caches stale values. */
  restart_required: boolean;
  keys: Record<string, EnvKeyStatus>;
}

export interface SetEnvResult {
  ok: boolean;
  written: string[];
  restart_required: boolean;
}

/** Result of `POST /api/setup/env/validate` — a live test of a credential
 *  combo against the real service (HA, Pushover, ntfy, Paperless). `ok=false`
 *  means the values were rejected; `detail` is a short human-readable reason. */
export interface ValidateResult {
  ok: boolean;
  detail: string;
}

// ----- API -----

export const setupApi = {
  getStatus: () => get<SetupStatus>('/api/setup/status'),
  getHardware: () => get<HardwareScan>('/api/setup/hardware'),
  complete: () => post<SetupCompleteResult>('/api/setup/complete'),
  getEnv: () => get<EnvStatus>('/api/setup/env'),
  setEnv: (values: Record<string, string>) =>
    post<SetEnvResult>('/api/setup/env', { values }),
  validateEnv: (service: string, values: Record<string, string>) =>
    post<ValidateResult>('/api/setup/env/validate', { service, values }),
};
