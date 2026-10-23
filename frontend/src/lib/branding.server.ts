// Server-only: the assistant's display name from the identity settings
// (/settings → Identity), so the title, wordmark, login heading and PWA name
// follow the install instead of being hard-coded. Same direct-to-orchestrator
// fetch as features.server.ts.

export const DEFAULT_ASSISTANT_NAME = 'Jess';

export async function getAssistantName(): Promise<string> {
  const base = process.env.ORCHESTRATOR_URL || 'http://localhost:8888';
  const token = process.env.API_TOKEN || '';
  try {
    const res = await fetch(`${base}/api/config/identity`, {
      headers: { Authorization: `Bearer ${token}` },
      next: { revalidate: 60 },
    });
    if (!res.ok) throw new Error(`identity ${res.status}`);
    const name = ((await res.json()) as { assistant_name?: unknown }).assistant_name;
    return typeof name === 'string' && name.trim() ? name.trim().slice(0, 64) : DEFAULT_ASSISTANT_NAME;
  } catch (err) {
    console.warn('[branding] failed to read assistant name, using default:', err);
    return DEFAULT_ASSISTANT_NAME;
  }
}
