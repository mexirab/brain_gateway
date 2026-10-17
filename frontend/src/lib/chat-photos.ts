import { resizeImage } from './image-resize';

/** The orchestrator's vision step reads at most 3 images per request. */
export const MAX_CHAT_PHOTOS = 3;

// Small photos keep the request and the vision call fast: 3 at this cap are
// ~800 KB of base64. (The orchestrator accepts chat bodies up to
// DOCUMENT_MAX_SIZE_MB — /v1/chat/completions is in _LARGE_UPLOAD_PATHS.)
const MAX_PHOTO_BYTES = 200_000;

// First pass keeps detail for reading labels/screens; the second only runs
// for unusually busy photos that blow the byte cap.
const PASSES: Array<[maxSide: number, quality: number]> = [
  [1280, 0.8],
  [1024, 0.65],
];

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
 * request. Re-encoding strips EXIF (incl. GPS). Throws if the browser can't
 * decode the image or it is still too large after the smaller pass — never
 * falls back to sending the original.
 */
export async function preparePhoto(file: Blob): Promise<string> {
  for (const [maxSide, quality] of PASSES) {
    const jpeg = await resizeImage(file, maxSide, quality);
    if (jpeg.size <= MAX_PHOTO_BYTES) return blobToDataUri(jpeg);
  }
  throw new Error('photo too large');
}
