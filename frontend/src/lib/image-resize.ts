/**
 * Downscale a phone photo before upload: long side ≤ `maxSide`, re-encoded as
 * JPEG. Cuts a 4–5 MB camera shot to a few hundred KB, which makes the upload
 * and the vision call faster, turns any format the browser can decode
 * (including iPhone HEIC in Safari) into JPEG, and drops EXIF (incl. GPS).
 *
 * Throws if the browser can't decode the image — callers must NOT fall back to
 * uploading the original, which would keep its location metadata.
 */
export async function resizeImage(file: Blob, maxSide = 2048, quality = 0.85): Promise<Blob> {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  try {
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('canvas unavailable');
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
    if (!blob) throw new Error('could not encode photo');
    return blob;
  } finally {
    bitmap.close?.();
    // iOS Safari has a hard total canvas-memory budget; release it now.
    canvas.width = 0;
    canvas.height = 0;
  }
}

/** Resize several photos one at a time (parallel decodes of 12 MP photos
 *  blow through iOS memory limits). */
export async function resizeAll(files: Blob[], maxSide = 2048): Promise<Blob[]> {
  const out: Blob[] = [];
  for (const f of files) out.push(await resizeImage(f, maxSide));
  return out;
}
