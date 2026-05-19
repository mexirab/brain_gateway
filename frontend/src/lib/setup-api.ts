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

export interface HardwareScan {
  ok: boolean;
  available: boolean;
  /** Present only when available — the cached detect_hardware.sh --json scan. */
  scan?: Record<string, unknown>;
  /** Present only when unavailable — operator instruction to produce the scan. */
  hint?: string;
}

export interface SetupCompleteResult {
  ok: boolean;
  setup_completed: boolean;
  completed_at: string | null;
}

// ----- API -----

export const setupApi = {
  getStatus: () => get<SetupStatus>('/api/setup/status'),
  getHardware: () => get<HardwareScan>('/api/setup/hardware'),
  complete: () => post<SetupCompleteResult>('/api/setup/complete'),
};
