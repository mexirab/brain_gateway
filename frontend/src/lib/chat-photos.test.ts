import { beforeEach, describe, expect, it, vi } from 'vitest';

// A plain function keeps the call tracking explicit (passes tried, in order).
type Resize = (file: Blob, maxSide: number, quality: number) => Promise<Blob>;
let impl: Resize;
let calls: Array<[number, number]> = [];
vi.mock('./image-resize', () => ({
  resizeImage: (file: Blob, maxSide: number, quality: number) => {
    calls.push([maxSide, quality]);
    return impl(file, maxSide, quality);
  },
}));

import { MAX_PHOTO_BYTES, PhotoTooLargeError, preparePhoto } from './chat-photos';

const jpegOf = (bytes: number) => new Blob([new Uint8Array(bytes)], { type: 'image/jpeg' });

describe('preparePhoto', () => {
  beforeEach(() => {
    calls = [];
  });

  it('accepts a typical busy phone photo (~500 KB at 1280px) on the first pass', async () => {
    impl = async () => jpegOf(500_000);
    const uri = await preparePhoto(new Blob(['x']));
    expect(uri.startsWith('data:image/jpeg;base64,')).toBe(true);
    expect(calls).toEqual([[1280, 0.8]]);
  });

  it('falls back to the smaller pass when the first is over the cap', async () => {
    impl = async (_f, maxSide) => jpegOf(maxSide === 1280 ? MAX_PHOTO_BYTES + 1 : 900_000);
    await expect(preparePhoto(new Blob(['x']))).resolves.toMatch(/^data:image\/jpeg;base64,/);
    expect(calls).toEqual([[1280, 0.8], [1024, 0.65]]);
  });

  it('throws PhotoTooLargeError when both passes are over the cap', async () => {
    impl = async () => jpegOf(MAX_PHOTO_BYTES + 1);
    await expect(preparePhoto(new Blob(['x']))).rejects.toBeInstanceOf(PhotoTooLargeError);
  });

  it('passes a decode failure through as a different error (never sends the original)', async () => {
    impl = async () => {
      throw new Error('The source image could not be decoded.');
    };
    const err = await preparePhoto(new Blob(['x'])).catch((e: unknown) => e);
    expect(err).not.toBeInstanceOf(PhotoTooLargeError);
    expect(String(err)).toContain('could not be decoded');
    expect(calls).toEqual([[1280, 0.8]]);
  });
});
