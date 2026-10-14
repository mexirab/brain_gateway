'use client';

import { useCallback, useState } from 'react';
import { ScanBarcode } from 'lucide-react';
import { Button } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import {
  CATEGORIES,
  LEVELS,
  LOCATIONS,
  pantryApi,
  type Category,
  type Level,
  type PantryItem,
  type PantryLocation,
} from '@/lib/pantry-api';
import BarcodeScanner from './BarcodeScanner';

const COUNTABLE_HINT = 'Leave blank for things you use up gradually (milk, rice).';

export default function AddItemForm({
  defaultLocation,
  onAdded,
  onCancel,
}: {
  defaultLocation: PantryLocation;
  onAdded: (item: PantryItem, merged: boolean) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState('');
  const [location, setLocation] = useState<PantryLocation>(defaultLocation);
  const [category, setCategory] = useState<Category>('other');
  const [count, setCount] = useState('');
  const [level, setLevel] = useState<Level>('full');
  const [staple, setStaple] = useState(false);
  const [barcode, setBarcode] = useState<string | null>(null);
  const [scanOpen, setScanOpen] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDetected = useCallback(async (code: string) => {
    setScanOpen(false);
    setBarcode(code);
    setNote(`Looking up ${code}…`);
    let res;
    try {
      res = await pantryApi.barcode(code);
    } catch {
      setNote("Couldn't look that up — type the name; the barcode is still saved.");
      return;
    }
    if (res.ok && res.name) {
      const found = res.name;
      setName(res.brand && !found.toLowerCase().includes(res.brand.toLowerCase()) ? `${res.brand} ${found}` : found);
      const cat = res.category;
      if (cat) {
        setCategory(cat);
        // Shelf-stable finds default to the pantry, frozen ones to the freezer.
        if (cat === 'frozen') setLocation('freezer');
        else if (!['dairy', 'protein', 'produce', 'prepared'].includes(cat)) {
          setLocation((loc) => (loc === 'fridge' ? 'pantry' : loc));
        }
      }
      setNote(`Found${res.quantity ? ` (${res.quantity})` : ''}. Check the name, then save.`);
    } else if (res.reason === 'disabled') {
      setNote('Barcode lookup is off on this install — type the name; the barcode is still saved.');
    } else {
      setNote(`${res.error ?? 'Not found'}. The barcode is still saved with the item.`);
    }
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    setError(null);
    const qty = count.trim() === '' ? null : Number(count);
    try {
      const res = await pantryApi.add({
        name: name.trim(),
        location,
        category,
        track_mode: qty !== null ? 'count' : staple ? 'staple' : 'level',
        qty,
        level: qty !== null ? null : staple ? 'full' : level,
        barcode,
      });
      onAdded(res.item, res.merged);
    } catch (err) {
      setError(friendlyError(err, 'Couldn’t add that item.', { preferDetail: true }));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <form onSubmit={submit} className="glass p-4 space-y-3 border-success/20">
        <div className="flex gap-2">
          <input
            className="input flex-1 text-base md:text-sm"
            placeholder="Item name (e.g. chickpeas)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={80}
            autoFocus
            aria-label="Item name"
          />
          <Button type="button" variant="secondary" icon onClick={() => setScanOpen(true)} aria-label="Scan barcode">
            <ScanBarcode size={18} />
          </Button>
        </div>
        {note && <p className="text-xs text-info/80">{note}</p>}
        <div className="grid grid-cols-2 gap-2">
          <select
            className="input text-sm"
            value={location}
            onChange={(e) => setLocation(e.target.value as PantryLocation)}
            aria-label="Location"
          >
            {LOCATIONS.map((l) => (
              <option key={l} value={l}>
                {l[0].toUpperCase() + l.slice(1)}
              </option>
            ))}
          </select>
          <select
            className="input text-sm"
            value={category}
            onChange={(e) => setCategory(e.target.value as Category)}
            aria-label="Category"
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>
        <div className="flex gap-2 items-center">
          <input
            className="input w-28"
            type="number"
            min={0}
            inputMode="numeric"
            placeholder="How many?"
            value={count}
            onChange={(e) => setCount(e.target.value)}
            aria-label="Count"
            title={COUNTABLE_HINT}
          />
          {count.trim() === '' && !staple && (
            <select
              className="input text-sm flex-1"
              value={level}
              onChange={(e) => setLevel(e.target.value as Level)}
              aria-label="How full"
            >
              {LEVELS.filter((l) => l !== 'out').map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          )}
          {count.trim() === '' && (
            <label className="flex items-center gap-1 text-xs text-content-muted shrink-0">
              <input type="checkbox" checked={staple} onChange={(e) => setStaple(e.target.checked)} />
              staple
            </label>
          )}
        </div>
        <p className="text-[11px] text-content-muted">
          {COUNTABLE_HINT} Staples (spices, oil, salt) are just “have” or “out”.
        </p>
        {error && <p className="text-sm text-danger">{error}</p>}
        <div className="flex gap-2">
          <Button type="submit" variant="primary" className="flex-1" disabled={saving || !name.trim()}>
            {saving ? 'Saving…' : 'Add'}
          </Button>
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      </form>
      <BarcodeScanner open={scanOpen} onClose={() => setScanOpen(false)} onDetected={handleDetected} />
    </>
  );
}
