import type { HAEntity } from './types';

/** Entities that can't be acted on right now. Scenes report `unknown` until
 *  first activated, so for scenes only `unavailable` counts. */
export function isUnavailable(e: HAEntity): boolean {
  const domain = e.entity_id.split('.', 1)[0];
  if (e.state === 'unavailable') return true;
  return domain !== 'scene' && e.state === 'unknown';
}

export interface Section {
  /** Room name, or a domain label in "by type" view. */
  name: string;
  lights: HAEntity[];
  scenes: HAEntity[];
  media: HAEntity[];
  climate: HAEntity[];
  switches: HAEntity[];
  other: HAEntity[];
}

export interface HomeLayout {
  sections: Section[];
  /** Device config/diagnostic toggles (camera enabled, LED, …) — collapsed. */
  settings: HAEntity[];
  unavailable: HAEntity[];
}

export const OTHER_ROOM = 'Other';

const byName = (a: HAEntity, b: HAEntity) =>
  a.friendly_name.localeCompare(b.friendly_name, undefined, { numeric: true, sensitivity: 'base' });

/** Light groups (e.g. a whole-room group) first, then by name. */
const lightOrder = (a: HAEntity, b: HAEntity) =>
  Number(!!b.is_group) - Number(!!a.is_group) || byName(a, b);

function emptySection(name: string): Section {
  return { name, lights: [], scenes: [], media: [], climate: [], switches: [], other: [] };
}

function place(section: Section, e: HAEntity) {
  const domain = e.entity_id.split('.', 1)[0];
  if (domain === 'light') section.lights.push(e);
  else if (domain === 'scene') section.scenes.push(e);
  else if (domain === 'media_player') section.media.push(e);
  else if (domain === 'climate') section.climate.push(e);
  else if (domain === 'switch' || domain === 'fan') section.switches.push(e);
  else section.other.push(e);
}

function finish(section: Section): Section {
  section.lights.sort(lightOrder);
  for (const key of ['scenes', 'media', 'climate', 'switches', 'other'] as const) section[key].sort(byName);
  return section;
}

export function sectionSize(s: Section): number {
  return s.lights.length + s.scenes.length + s.media.length + s.climate.length + s.switches.length + s.other.length;
}

export function lightsOn(s: Section): HAEntity[] {
  return s.lights.filter((l) => l.state === 'on');
}

/** Split entities into the page's sections. `mode='rooms'` groups by HA area
 *  (unassigned → "Other", last); `mode='type'` gives one section per kind. */
export function buildLayout(
  controllable: Record<string, HAEntity[]>,
  mode: 'rooms' | 'type',
): HomeLayout {
  const settings: HAEntity[] = [];
  const unavailable: HAEntity[] = [];
  const sections = new Map<string, Section>();

  for (const entities of Object.values(controllable)) {
    for (const e of entities) {
      if (e.category === 'config' || e.category === 'diagnostic') {
        settings.push(e);
        continue;
      }
      if (isUnavailable(e)) {
        unavailable.push(e);
        continue;
      }
      const key = mode === 'rooms' ? e.area || OTHER_ROOM : 'all';
      if (!sections.has(key)) sections.set(key, emptySection(key));
      place(sections.get(key)!, e);
    }
  }

  let out: Section[];
  if (mode === 'rooms') {
    out = [...sections.values()].map(finish).sort((a, b) => {
      if (a.name === OTHER_ROOM) return 1;
      if (b.name === OTHER_ROOM) return -1;
      return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
    });
  } else {
    // By type: one section per kind, common controls first.
    const all = sections.get('all') ? finish(sections.get('all')!) : emptySection('all');
    const kinds: [string, keyof Omit<Section, 'name'>][] = [
      ['Lights', 'lights'],
      ['Scenes', 'scenes'],
      ['Media', 'media'],
      ['Switches', 'switches'],
      ['Climate', 'climate'],
      ['Other', 'other'],
    ];
    out = kinds
      .filter(([, key]) => all[key].length > 0)
      .map(([name, key]) => ({ ...emptySection(name), [key]: all[key] }));
  }

  return { sections: out, settings: settings.sort(byName), unavailable: unavailable.sort(byName) };
}

/** HA brightness (0–255) → percent, clamped to 1–100 for an on light. */
export function brightnessPct(b: number | null | undefined): number {
  if (typeof b !== 'number') return 100;
  return Math.max(1, Math.min(100, Math.round((b / 255) * 100)));
}

/** Plain-language message for a failed /api/ha/command call. */
export function commandErrorMessage(err: unknown): string {
  const m = err instanceof Error ? /^API (\d+)/.exec(err.message) : null;
  const status = m ? Number(m[1]) : 0;
  if (status === 403) return 'Not allowed from the dashboard';
  if (status === 401) return 'Signed out — log in again';
  if (status === 404) return 'Device not found — refresh';
  if (status === 502 || status === 503 || status === 504) return 'Home Assistant didn’t respond';
  if (status === 422 || status === 400) return 'Request rejected';
  return 'Couldn’t reach the server';
}

/** "Living room floor lamp" → "Floor lamp" inside the Living room card.
 *  Leaves the name alone when stripping would leave nothing. */
export function stripRoom(name: string, room?: string): string {
  const trimmed = name.trim();
  if (!room) return trimmed;
  const prefix = room.trim().toLowerCase() + ' ';
  if (!trimmed.toLowerCase().startsWith(prefix)) return trimmed;
  const rest = trimmed.slice(prefix.length).trim();
  return rest ? rest.charAt(0).toUpperCase() + rest.slice(1) : trimmed;
}
