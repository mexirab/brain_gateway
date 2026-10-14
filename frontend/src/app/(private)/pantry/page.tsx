'use client';

import { useEffect, useState } from 'react';
import { Refrigerator, X } from 'lucide-react';
import { Button, Card } from '@/components/ui';
import InventoryTab from '@/components/pantry/InventoryTab';
import ScanTab from '@/components/pantry/ScanTab';

type Tab = 'inventory' | 'scan';

export default function PantryPage() {
  const [tab, setTab] = useState<Tab>('inventory');
  const [notice, setNotice] = useState<string | null>(null);
  // Remount the inventory after a scan commit so it reloads.
  const [inventoryKey, setInventoryKey] = useState(0);

  // Deep link: /pantry?tab=scan (e.g. from a future "weekly fridge check" nudge).
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get('tab') === 'scan') setTab('scan');
  }, []);

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold text-content-primary flex items-center gap-3">
        <Refrigerator size={24} className="text-success" />
        Pantry
      </h1>

      <div className="flex gap-1 p-1 rounded-lg bg-surface-raised/40 border border-line-subtle" role="tablist">
        {(['inventory', 'scan'] as Tab[]).map((t) => (
          <button
            key={t}
            role="tab"
            id={`pantry-tab-${t}`}
            aria-selected={tab === t}
            aria-controls={`pantry-panel-${t}`}
            onClick={() => setTab(t)}
            className={`flex-1 py-2 rounded-md text-sm transition-colors ${
              tab === t ? 'bg-brand/25 text-content-primary' : 'text-content-muted hover:text-content-primary'
            }`}
          >
            {t === 'inventory' ? 'What I have' : 'Photo stock-take'}
          </button>
        ))}
      </div>

      {notice && (
        <Card padding="none" className="p-3 text-sm text-success border-success/30 flex items-start gap-2">
          <span className="flex-1">{notice}</span>
          <Button icon size="sm" variant="ghost" onClick={() => setNotice(null)} aria-label="Dismiss">
            <X size={16} />
          </Button>
        </Card>
      )}

      {/* Both panels stay mounted so staged photos and review edits survive a
          quick look at the other tab. */}
      <div
        role="tabpanel"
        id="pantry-panel-inventory"
        aria-labelledby="pantry-tab-inventory"
        hidden={tab !== 'inventory'}
      >
        <InventoryTab key={inventoryKey} shown={tab === 'inventory'} onStartScan={() => setTab('scan')} />
      </div>
      <div role="tabpanel" id="pantry-panel-scan" aria-labelledby="pantry-tab-scan" hidden={tab !== 'scan'}>
        <ScanTab
          onCommitted={(msg) => {
            setNotice(msg);
            setInventoryKey((k) => k + 1);
            setTab('inventory');
          }}
        />
      </div>
    </div>
  );
}
