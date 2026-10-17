import { describe, expect, it } from 'vitest';
import { asAttachment } from './content-disposition';

describe('asAttachment', () => {
  it.each([
    [null, 'attachment'],
    ['inline', 'attachment'],
    ['inline; filename="a.pdf"', 'attachment; filename="a.pdf"'],
    ['attachment; filename="a.pdf"; filename*=utf-8\'\'a.pdf', "attachment; filename=\"a.pdf\"; filename*=utf-8''a.pdf"],
    ['inline-foo; filename=x', 'attachment; filename=x'],
    ['form-data; name=x', 'attachment; name=x'],
    ['foo.pdf', 'attachment'],
  ])('%j -> %j', (input, expected) => {
    expect(asAttachment(input)).toBe(expected);
  });
});
