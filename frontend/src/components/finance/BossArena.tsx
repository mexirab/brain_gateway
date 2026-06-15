'use client';

import { useState } from 'react';
import { Swords, Shield, Loader2, TrendingUp, Wallet } from 'lucide-react';
import { formatCurrency } from '@/lib/finance-utils';

interface Props {
  type: 'bonus' | 'espp';
  defaultInvestPercent: number;
  onDefeatBoss: (data: {
    type: 'bonus' | 'espp';
    amount: number;
    invest_percent: number;
  }) => Promise<void>;
}

export default function BossArena({ type, defaultInvestPercent, onDefeatBoss }: Props) {
  const [amount, setAmount] = useState('');
  const [investPercent, setInvestPercent] = useState(defaultInvestPercent);
  const [busy, setBusy] = useState(false);
  const [defeated, setDefeated] = useState(false);

  const amountNum = parseFloat(amount) || 0;
  const investAmount = amountNum * (investPercent / 100);
  const spendAmount = amountNum - investAmount;

  const isBoss = type === 'bonus' ? 'Bonus' : 'ESPP';
  const bossTheme =
    type === 'bonus'
      ? { border: 'border-accent-gold/30', iconBg: 'bg-accent-gold/15', iconText: 'text-accent-gold' }
      : { border: 'border-accent-violet/30', iconBg: 'bg-accent-violet/15', iconText: 'text-accent-violet' };

  async function handleDefeat() {
    if (amountNum <= 0) return;
    setBusy(true);
    try {
      await onDefeatBoss({ type, amount: amountNum, invest_percent: investPercent });
      setDefeated(true);
    } finally {
      setBusy(false);
    }
  }

  if (defeated) {
    return (
      <div className="glass p-8 border border-accent-gold/40 text-center">
        <div className="text-5xl mb-3">⚔️</div>
        <h2 className="text-xl font-bold text-accent-gold">Boss Defeated!</h2>
        <p className="text-sm text-content-secondary mt-2">
          {isBoss} windfall of {formatCurrency(amountNum)} split successfully
        </p>
        <div className="flex justify-center gap-6 mt-4">
          <div>
            <p className="text-lg font-bold text-success">{formatCurrency(investAmount)}</p>
            <p className="text-xs text-content-muted">Invested</p>
          </div>
          <div>
            <p className="text-lg font-bold text-brand">{formatCurrency(spendAmount)}</p>
            <p className="text-xs text-content-muted">Guilt-Free Spend</p>
          </div>
        </div>
        <p className="text-sm text-accent-gold/80 mt-4 font-medium">+200 XP earned!</p>
      </div>
    );
  }

  return (
    <div className={`glass p-6 border ${bossTheme.border}`}>
      {/* Boss header */}
      <div className="flex items-center gap-3 mb-6">
        <div className={`p-3 rounded-xl ${bossTheme.iconBg}`}>
          <Swords size={28} className={bossTheme.iconText} />
        </div>
        <div>
          <h2 className="text-lg font-bold text-content-primary">
            {isBoss} Boss Battle
          </h2>
          <p className="text-sm text-content-muted">
            {type === 'bonus'
              ? 'Split your bonus wisely to defeat the boss'
              : 'ESPP windfall — recommended 67% invest / 33% spend'}
          </p>
        </div>
      </div>

      {/* Amount input */}
      <div className="mb-5">
        <label className="block text-xs text-content-secondary mb-1.5">
          Windfall Amount
        </label>
        <div className="relative">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-content-muted">$</span>
          <input
            type="number"
            placeholder={type === 'bonus' ? '8231' : '8237'}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            className="input text-lg pl-7 pr-3 py-3 font-mono"
            min="0"
            step="1"
          />
        </div>
      </div>

      {/* Invest slider */}
      <div className="mb-5">
        <div className="flex justify-between text-xs text-content-secondary mb-2">
          <span className="flex items-center gap-1">
            <TrendingUp size={12} />
            Invest: {investPercent}%
          </span>
          <span className="flex items-center gap-1">
            <Wallet size={12} />
            Spend: {100 - investPercent}%
          </span>
        </div>
        <input
          type="range"
          min="0"
          max="100"
          step="1"
          value={investPercent}
          onChange={(e) => setInvestPercent(parseInt(e.target.value))}
          className="w-full accent-brand h-2"
        />

        {/* Split preview */}
        {amountNum > 0 && (
          <div className="flex justify-between mt-3">
            <div className="text-center flex-1">
              <p className="text-lg font-bold text-success font-mono">
                {formatCurrency(investAmount)}
              </p>
              <p className="text-xs text-content-muted">To investments</p>
            </div>
            <div className="w-px bg-line mx-3" />
            <div className="text-center flex-1">
              <p className="text-lg font-bold text-brand font-mono">
                {formatCurrency(spendAmount)}
              </p>
              <p className="text-xs text-content-muted">Guilt-free spend</p>
            </div>
          </div>
        )}
      </div>

      {/* Quick-set buttons */}
      <div className="flex gap-2 mb-5">
        {[
          { label: '50/50', pct: 50 },
          { label: '67/33', pct: 67 },
          { label: '80/20', pct: 80 },
          { label: '100%', pct: 100 },
        ].map((opt) => (
          <button
            key={opt.pct}
            onClick={() => setInvestPercent(opt.pct)}
            className={`flex-1 py-1.5 text-xs font-medium rounded-lg border transition-colors ${
              investPercent === opt.pct
                ? 'border-brand bg-brand/10 text-brand'
                : 'border-line text-content-secondary hover:border-line-strong'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* Defeat button */}
      <button
        onClick={handleDefeat}
        disabled={busy || amountNum <= 0}
        className="w-full py-3 bg-gradient-to-r from-accent-gold to-accent-flame hover:from-accent-gold/90 hover:to-accent-flame/90 disabled:opacity-40 text-white font-bold text-sm rounded-xl transition-all flex items-center justify-center gap-2 shadow-lg shadow-accent-gold/10"
      >
        {busy ? (
          <Loader2 size={18} className="animate-spin" />
        ) : (
          <>
            <Shield size={18} />
            Defeat the {isBoss} Boss
          </>
        )}
      </button>

      {/* XP reward note */}
      <p className="text-center text-xs text-content-muted mt-2">
        +200 XP • Jess will announce your victory
      </p>
    </div>
  );
}
