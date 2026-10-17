import { describe, it, expect } from 'vitest';
import type { HAEntity } from './types';
import { brightnessPct, buildLayout, commandErrorMessage, isUnavailable, OTHER_ROOM, stripRoom } from './home-groups';

const e = (entity_id: string, extra: Partial<HAEntity> = {}): HAEntity => ({
  entity_id,
  friendly_name: entity_id.split('.')[1].replace(/_/g, ' '),
  state: 'off',
  area: null,
  category: null,
  ...extra,
});

const controllable = {
  light: [
    e('light.lamp_b', { area: 'Den', state: 'on' }),
    e('light.den_group', { area: 'Den', is_group: true }),
    e('light.lamp_a', { area: 'Den' }),
    e('light.dead', { area: 'Den', state: 'unavailable' }),
    e('light.nowhere'),
  ],
  scene: [e('scene.den_relax', { area: 'Den', state: 'unknown' })],
  switch: [
    e('switch.cam_enabled', { area: 'Attic', category: 'config', state: 'on' }),
    e('switch.kettle', { area: 'Attic', state: 'unknown' }),
  ],
  media_player: [e('media_player.speaker', { area: 'Attic', state: 'playing' })],
};

describe('buildLayout (rooms)', () => {
  const layout = buildLayout(controllable, 'rooms');

  it('groups by area, alphabetically, unassigned last', () => {
    expect(layout.sections.map((s) => s.name)).toEqual(['Attic', 'Den', OTHER_ROOM]);
  });

  it('puts light groups first, then by name', () => {
    const den = layout.sections.find((s) => s.name === 'Den')!;
    expect(den.lights.map((l) => l.entity_id)).toEqual(['light.den_group', 'light.lamp_a', 'light.lamp_b']);
  });

  it('keeps never-activated scenes (state unknown) usable', () => {
    const den = layout.sections.find((s) => s.name === 'Den')!;
    expect(den.scenes.map((s) => s.entity_id)).toEqual(['scene.den_relax']);
  });

  it('moves config entities to settings and offline ones to unavailable', () => {
    expect(layout.settings.map((x) => x.entity_id)).toEqual(['switch.cam_enabled']);
    expect(layout.unavailable.map((x) => x.entity_id).sort()).toEqual(['light.dead', 'switch.kettle']);
    const attic = layout.sections.find((s) => s.name === 'Attic')!;
    expect(attic.switches).toEqual([]);
    expect(attic.media.map((m) => m.entity_id)).toEqual(['media_player.speaker']);
  });
});

describe('buildLayout (type)', () => {
  it('gives one section per kind, common controls first', () => {
    const layout = buildLayout(controllable, 'type');
    expect(layout.sections.map((s) => s.name)).toEqual(['Lights', 'Scenes', 'Media']);
    expect(layout.sections[0].lights).toHaveLength(4);
  });
});

describe('helpers', () => {
  it('isUnavailable treats unknown as offline except for scenes', () => {
    expect(isUnavailable(e('scene.x', { state: 'unknown' }))).toBe(false);
    expect(isUnavailable(e('scene.x', { state: 'unavailable' }))).toBe(true);
    expect(isUnavailable(e('light.x', { state: 'unknown' }))).toBe(true);
  });

  it('brightnessPct maps 0-255 to 1-100', () => {
    expect(brightnessPct(255)).toBe(100);
    expect(brightnessPct(128)).toBe(50);
    expect(brightnessPct(0)).toBe(1);
    expect(brightnessPct(null)).toBe(100);
  });

  it('commandErrorMessage maps proxy statuses to plain language', () => {
    expect(commandErrorMessage(new Error('API 403: Forbidden'))).toMatch(/not allowed/i);
    expect(commandErrorMessage(new Error('API 502: Bad Gateway'))).toMatch(/didn’t respond/);
    expect(commandErrorMessage(new TypeError('Failed to fetch'))).toMatch(/server/);
  });
});

describe('stripRoom', () => {
  it('drops a leading room name, case-insensitively, and recapitalises', () => {
    expect(stripRoom('Living room floor lamp 1', 'Living Room')).toBe('Floor lamp 1');
    expect(stripRoom('Office', 'Office')).toBe('Office');
    expect(stripRoom('Desk lamp', 'Office')).toBe('Desk lamp');
    expect(stripRoom('Desk lamp')).toBe('Desk lamp');
  });
});
