'use client';

import { useEffect, useRef, useState } from 'react';
import { Camera } from 'lucide-react';
import { Button, Modal } from '@/components/ui';
import { resizeImage } from '@/lib/image-resize';

// Grocery barcodes only. Restricting formats makes decoding faster and avoids
// QR codes on packaging being read as a product.
async function makeReader() {
  const [{ BrowserMultiFormatReader }, { BarcodeFormat, DecodeHintType }] = await Promise.all([
    import('@zxing/browser'),
    import('@zxing/library'),
  ]);
  const hints = new Map();
  hints.set(DecodeHintType.POSSIBLE_FORMATS, [
    BarcodeFormat.EAN_13,
    BarcodeFormat.EAN_8,
    BarcodeFormat.UPC_A,
    BarcodeFormat.UPC_E,
  ]);
  hints.set(DecodeHintType.TRY_HARDER, true);
  return new BrowserMultiFormatReader(hints);
}

function liveCameraAvailable(): boolean {
  return typeof window !== 'undefined' && window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;
}

/**
 * Barcode capture. Live camera needs a secure context (HTTPS or localhost);
 * the dashboard is often opened over plain HTTP on the LAN, where
 * getUserMedia doesn't exist — then the user takes a photo of the barcode
 * instead and it is decoded from the still image. Typing the digits always
 * works as a last resort.
 *
 * Render this OUTSIDE any <form>: Modal doesn't portal, and its buttons and
 * the manual-entry form would otherwise submit the parent form.
 */
export default function BarcodeScanner({
  open,
  onClose,
  onDetected,
}: {
  open: boolean;
  onClose: () => void;
  onDetected: (code: string) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [liveFailed, setLiveFailed] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [manual, setManual] = useState('');
  const [busy, setBusy] = useState(false);
  const live = open && !liveFailed && liveCameraAvailable();

  useEffect(() => {
    if (!open) {
      setMessage(null);
      setManual('');
      setLiveFailed(false);
    }
  }, [open]);

  useEffect(() => {
    if (!live) return;
    let stopped = false;
    let controls: { stop: () => void } | null = null;
    (async () => {
      try {
        const reader = await makeReader();
        if (stopped) return;
        if (!videoRef.current) throw new Error('video element not mounted');
        controls = await reader.decodeFromConstraints(
          { video: { facingMode: 'environment' } },
          videoRef.current,
          (result) => {
            if (result && !stopped) {
              stopped = true;
              controls?.stop();
              onDetected(result.getText());
            }
          },
        );
        if (stopped) controls.stop();
      } catch {
        if (!stopped) {
          setLiveFailed(true);
          setMessage('Camera not available — take a photo of the barcode instead.');
        }
      }
    })();
    return () => {
      stopped = true;
      controls?.stop();
    };
  }, [live, onDetected]);

  const handlePhoto = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setBusy(true);
    setMessage(null);
    let url: string | null = null;
    try {
      // Decode a downscaled copy: zxing on a 12 MP still freezes the page.
      url = URL.createObjectURL(await resizeImage(file, 1600));
      const reader = await makeReader();
      const result = await reader.decodeFromImageUrl(url);
      onDetected(result.getText());
    } catch {
      setMessage("Couldn't read a barcode in that photo. Get closer, keep it flat and sharp, or type the numbers.");
    } finally {
      if (url) URL.revokeObjectURL(url);
      setBusy(false);
    }
  };

  const digits = manual.replace(/\D/g, '');
  const validDigits = digits.length >= 8 && digits.length <= 14;

  return (
    <Modal open={open} onClose={onClose} title="Scan a barcode" size="md">
      <div className="space-y-4">
        {live ? (
          <video ref={videoRef} className="w-full rounded-lg bg-black aspect-video object-cover" muted playsInline />
        ) : (
          <label
            className={`flex items-center justify-center gap-2 px-4 py-6 border rounded-lg cursor-pointer focus-within:ring-2 focus-within:ring-brand ${
              busy ? 'opacity-60 cursor-wait' : 'bg-info/15 text-info border-info/30 hover:bg-info/25'
            }`}
          >
            <Camera size={18} />
            {busy ? 'Reading…' : 'Take a photo of the barcode'}
            <input
              type="file"
              accept="image/*"
              capture="environment"
              className="sr-only"
              onChange={handlePhoto}
              disabled={busy}
            />
          </label>
        )}
        <p className="text-sm text-warning min-h-[1.25rem]" aria-live="polite">
          {message}
        </p>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            e.stopPropagation();
            if (validDigits) onDetected(digits);
          }}
        >
          <input
            className="input flex-1 text-base md:text-sm"
            inputMode="numeric"
            placeholder="…or type the numbers"
            value={manual}
            onChange={(e) => setManual(e.target.value)}
            aria-label="Barcode number"
          />
          <Button type="submit" variant="secondary" disabled={!validDigits}>
            Look up
          </Button>
        </form>
      </div>
    </Modal>
  );
}
