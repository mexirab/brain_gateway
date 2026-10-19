import { resizeImage } from './image-resize';

/** The orchestrator's vision step reads at most 3 images per request. */
export const MAX_CHAT_PHOTOS = 3;

// Per-photo backstop, not a target: a busy 12 MP iPhone shot re-encoded at
// 1280px is often 300–600 KB, and a 200 KB cap rejected real photos. The
// orchestrator accepts chat bodies up to DOCUMENT_MAX_SIZE_MB
// (/v1/chat/completions is in _LARGE_UPLOAD_PATHS) and vision images up to
// VISION_MAX_IMAGE_SIZE (10 MB), so 3 photos at this cap (~6 MB of base64)
// still fit with room to spare.
export const MAX_PHOTO_BYTES = 1_500_000;

// First pass keeps detail for reading labels/screens; the second only runs
// for unusually busy photos that blow the byte cap.
const PASSES: Array<[maxSide: number, quality: number]> = [
  [1280, 0.8],
  [1024, 0.65],
];

/** The photo decoded fine but stays over MAX_PHOTO_BYTES at the smallest pass. */
export class PhotoTooLargeError extends Error {
  constructor() {
    super('photo too large');
    this.name = 'PhotoTooLargeError';
  }
}

function blobToDataUri(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error ?? new Error('could not read photo'));
    reader.readAsDataURL(blob);
  });
}

/**
 * Turn a picked/captured photo into a small JPEG data URI for the chat
 * request. Re-encoding strips EXIF (incl. GPS). Throws PhotoTooLargeError if
 * it is still too large after the smaller pass, or the decode/encode error if
 * the browser can't open the image — never falls back to sending the original.
 */
export async function preparePhoto(file: Blob): Promise<string> {
  for (const [maxSide, quality] of PASSES) {
    const jpeg = await resizeImage(file, maxSide, quality);
    if (jpeg.size <= MAX_PHOTO_BYTES) return blobToDataUri(jpeg);
  }
  throw new PhotoTooLargeError();
}
