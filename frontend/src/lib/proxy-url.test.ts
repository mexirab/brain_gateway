import { describe, expect, it } from 'vitest';
import { buildUpstreamUrl } from './proxy-url';

const BASE = 'http://orchestrator:8888';

// Segments as Next delivers them: already percent-decoded.
describe('buildUpstreamUrl', () => {
  it.each([
    [['health'], '', 'http://orchestrator:8888/health'],
    [['api', 'config', 'personal-facts'], '', 'http://orchestrator:8888/api/config/personal-facts'],
    [['v1', 'chat', 'completions'], '', 'http://orchestrator:8888/v1/chat/completions'],
    [['api', 'tasks'], '?status=open', 'http://orchestrator:8888/api/tasks?status=open'],
    [['api', 'workouts', 'exercise-library', 'Barbell_Squat', '0.jpg'], '?v=abc1234',
      'http://orchestrator:8888/api/workouts/exercise-library/Barbell_Squat/0.jpg?v=abc1234'],
    [['api', 'pantry', 'barcode', '0 12?#%'], '', 'http://orchestrator:8888/api/pantry/barcode/0%2012%3F%23%25'],
    [['api', 'ha', 'light.kitchen'], '', 'http://orchestrator:8888/api/ha/light.kitchen'],
    // Next decodes once, so a double-encoded slash arrives as literal text
    [['api', '%2F'], '', 'http://orchestrator:8888/api/%252F'],
    [['api', 'evil.example:80@x'], '', 'http://orchestrator:8888/api/evil.example%3A80%40x'],
    [['api', '／evil'], '', 'http://orchestrator:8888/api/%EF%BC%8Fevil'],
    [['api', 'tasks'], '?a=1#@evil.example/', 'http://orchestrator:8888/api/tasks?a=1%23@evil.example/'],
  ])('%j%s -> %s', (segments, search, expected) => {
    expect(buildUpstreamUrl(segments, search, BASE)?.toString()).toBe(expected);
  });

  it('ignores any path on the base URL, like the old new URL(path, base)', () => {
    expect(buildUpstreamUrl(['health'], '', 'http://orchestrator:8888/prefix/')?.toString())
      .toBe('http://orchestrator:8888/health');
  });

  it.each([
    // the reported leak: a decoded %2F / %5C turning into //host
    [['/evil.example', 'x']],
    [['/', 'evil.example', 'x']],
    [['\\evil.example', 'x']],
    [['api', '/evil.example']],
    [['api', 'x\\..\\..', 'y']],
    // dot-segments and empties
    [['api', '..', 'metrics']],
    [['api', '.', 'x']],
    [['api', '', 'x']],
    [['..', 'metrics']],
    // control characters
    [['api', 'x\ty']],
    [['api', 'x\ny']],
    [['api', 'x\u0000']],
    // roots the dashboard never calls
    [[]],
    [['metrics']],
    [['docs']],
    [['openapi.json']],
    [['http:', '', 'evil.example']],
    [['API', 'x']],
    // lone surrogate: encodeURIComponent throws
    [['api', '\ud800']],
  ])('refuses %j', (segments) => {
    expect(buildUpstreamUrl(segments, '', BASE)).toBeNull();
  });
});
