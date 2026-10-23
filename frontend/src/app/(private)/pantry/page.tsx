'use client';

import { useEffect, useState } from 'react';
import { Refrigerator, X } from 'lucide-react';
import { Button, Card } from '@/components/ui';
import InventoryTab from '@/components/pantry/InventoryTab';
import ScanTab from '@/components/pantry/ScanTab';
import PlanTab from '@/components/pantry/PlanTab';

type Tab = 'inventory' | 'scan' | 'plan';

const TAB_LABEL: Record<Tab, string> = { inventory: 'What I have', scan: 'Stock-take', plan: 'Meal plan' };

export default function PantryPage() {
  const [tab, setTab] = useState<Tab>('inventory');
  const [notice, setNotice] = useState<string | null>(null);
  // Remount the inventory after a scan commit so it reloads.
  const [inventoryKey, setInventoryKey] = useState(0);

  // Deep links: /pantry?tab=scan (weekly fridge check), /pantry?tab=plan (cooking reminders).
  useEffect(() => {
    const t = new URLSearchParams(window.location.search).get('tab');
    if (t === 'scan' || t === 'plan') setTab(t);
  }, []);

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold text-content-primary flex items-center gap-3">
        <Refrigerator size={24} className="text-success" />
        Pantry
      </h1>

      <div className="segmented" role="tablist">
        {(['inventory', 'plan', 'scan'] as Tab[]).map((t) => (
          <button
            key={t}
            role="tab"
            id={`pantry-tab-${t}`}
            aria-selected={tab === t}
            aria-controls={`pantry-panel-${t}`}
            onClick={() => setTab(t)}
            className={`segmented-item ${tab === t ? 'segmented-item-active' : ''}`}
          >
            {TAB_LABEL[t]}
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
      <div role="tabpanel" id="pantry-panel-plan" aria-labelledby="pantry-tab-plan" hidden={tab !== 'plan'}>
        <PlanTab shown={tab === 'plan'} />
      </div>
    </div>
  );
}
