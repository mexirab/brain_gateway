'use client';

import { useState } from 'react';
import { X, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui';

const ICON_OPTIONS = [
  { value: 'gamepad', label: '🎮 Gaming' },
  { value: 'monitor', label: '🖥️ Tech' },
  { value: 'plane', label: '✈️ Travel' },
  { value: 'car', label: '🚗 Vehicle' },
  { value: 'gift', label: '🎁 Gift' },
  { value: 'gem', label: '💎 Luxury' },
  { value: 'trophy', label: '🏆 Goal' },
  { value: 'target', label: '🎯 Other' },
];

interface Props {
  onClose: () => void;
  onCreate: (quest: {
    name: string;
    target_amount: number;
    monthly_carve: number;
    description?: string;
    icon?: string;
  }) => Promise<void>;
}

export default function SideQuestModal({ onClose, onCreate }: Props) {
  const [name, setName] = useState('');
  const [target, setTarget] = useState('');
  const [monthlyCarve, setMonthlyCarve] = useState('');
  const [description, setDescription] = useState('');
  const [icon, setIcon] = useState('trophy');
  const [busy, setBusy] = useState(false);

  async function handleCreate() {
    const targetAmount = parseFloat(target);
    if (!name.trim() || isNaN(targetAmount) || targetAmount <= 0) return;

    setBusy(true);
    try {
      await onCreate({
        name: name.trim(),
        target_amount: targetAmount,
        monthly_carve: parseFloat(monthlyCarve) || 0,
        description: description.trim() || undefined,
        icon,
      });
      onClose();
    } finally {
      setBusy(false);
    }
  }

  const carveNum = parseFloat(monthlyCarve) || 0;
  const targetNum = parseFloat(target) || 0;
  const monthsToGoal = carveNum > 0 && targetNum > 0 ? Math.ceil(targetNum / carveNum) : null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="glass border border-line w-full max-w-md mx-4 p-6 rounded-2xl shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-bold text-content-primary">New Side Quest</h2>
          <button
            onClick={onClose}
            className="text-content-muted hover:text-content-primary transition-colors"
          >
            <X size={20} />
          </button>
        </div>

        {/* Form */}
        <div className="space-y-4">
          <div>
            <label className="block text-xs text-content-secondary mb-1">Quest Name *</label>
            <input
              type="text"
              placeholder="e.g., RTX 6000 Pro"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="input"
              autoFocus
            />
          </div>

          <div>
            <label className="block text-xs text-content-secondary mb-1">Description</label>
            <input
              type="text"
              placeholder="Why do you want this?"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="input"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-content-secondary mb-1">Target Amount *</label>
              <div className="relative">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-content-muted text-sm">$</span>
                <input
                  type="number"
                  placeholder="0"
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                  className="input pl-7 pr-3 py-2"
                  min="0"
                  step="1"
                />
              </div>
            </div>
            <div>
              <label className="block text-xs text-content-secondary mb-1">Monthly Auto-Save</label>
              <div className="relative">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-content-muted text-sm">$</span>
                <input
                  type="number"
                  placeholder="0"
                  value={monthlyCarve}
                  onChange={(e) => setMonthlyCarve(e.target.value)}
                  className="input pl-7 pr-3 py-2"
                  min="0"
                  step="1"
                />
              </div>
            </div>
          </div>

          {monthsToGoal && (
            <p className="text-xs text-content-muted">
              At ${carveNum}/mo, you&apos;ll reach your goal in ~{monthsToGoal} month{monthsToGoal === 1 ? '' : 's'}
            </p>
          )}

          {carveNum > 0 && (
            <p className="text-xs text-accent-flame/80">
              This will reduce your monthly guilt-free budget by ${carveNum}
            </p>
          )}

          {/* Icon picker */}
          <div>
            <label className="block text-xs text-content-secondary mb-1.5">Icon</label>
            <div className="flex flex-wrap gap-1.5">
              {ICON_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setIcon(opt.value)}
                  className={`text-xs px-2.5 py-1 rounded-lg border transition-colors ${
                    icon === opt.value
                      ? 'border-brand bg-brand/10 text-brand'
                      : 'border-line text-content-secondary hover:border-line-strong'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-3 mt-6">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 text-sm text-content-secondary border border-line rounded-lg hover:border-line-strong transition-colors"
          >
            Cancel
          </button>
          <Button
            onClick={handleCreate}
            disabled={busy || !name.trim() || !target || parseFloat(target) <= 0}
            className="flex-1"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : 'Start Quest'}
          </Button>
        </div>
      </div>
    </div>
  );
}
