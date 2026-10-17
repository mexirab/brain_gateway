import { describe, expect, it } from 'vitest';
import { buildChatHistory, withPhotoMarker, userTurnContent } from './chat-history';

const IMG = 'data:image/jpeg;base64,AAAA';

describe('withPhotoMarker', () => {
  it('leaves text alone without photos', () => {
    expect(withPhotoMarker('hi', 0)).toBe('hi');
  });
  it('marks one or several photos', () => {
    expect(withPhotoMarker('what is it?', 1)).toBe('[Shared a photo] what is it?');
    expect(withPhotoMarker('', 3)).toBe('[Shared 3 photos]');
  });
});

describe('userTurnContent', () => {
  it('is plain text without photos', () => {
    expect(userTurnContent('hello', [])).toBe('hello');
  });
  it('sends text then image parts', () => {
    expect(userTurnContent('what is this?', [IMG])).toEqual([
      { type: 'text', text: 'what is this?' },
      { type: 'image_url', image_url: { url: IMG } },
    ]);
  });
  it('sends a photo-only turn with no empty text part', () => {
    expect(userTurnContent('', [IMG])).toEqual([{ type: 'image_url', image_url: { url: IMG } }]);
  });
});

describe('buildChatHistory', () => {
  it('turns earlier photos into markers and never resends them', () => {
    const history = buildChatHistory(
      [
        { role: 'user', content: '', photoCount: 1 },
        { role: 'assistant', content: 'A red mug.' },
      ],
      'and this one?',
      [IMG],
    );
    expect(history).toEqual([
      { role: 'user', content: '[Shared a photo]' },
      { role: 'assistant', content: 'A red mug.' },
      {
        role: 'user',
        content: [
          { type: 'text', text: 'and this one?' },
          { type: 'image_url', image_url: { url: IMG } },
        ],
      },
    ]);
    expect(JSON.stringify(history.slice(0, -1))).not.toContain('base64');
  });

  it('drops error and blank turns and labels announcements', () => {
    const history = buildChatHistory(
      [
        { role: 'assistant', content: 'Sorry — failed', error: true },
        { role: 'assistant', content: '   ' },
        { role: 'assistant', content: 'Take your meds', announcementType: 'reminder' },
      ],
      'ok',
      [],
    );
    expect(history).toEqual([
      { role: 'assistant', content: '[Jess announced - reminder]: Take your meds' },
      { role: 'user', content: 'ok' },
    ]);
  });
});
